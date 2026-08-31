"""Safe planning and transactional export of allowlisted QLIE scripts."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..models import QlieScriptExportItem, QlieScriptExportPlan, QlieScriptExportResult
from .archive import inspect_filepack_toc
from .detector import REPORT_SCHEMA_VERSION
from .payload import read_filepack_entry


DEFAULT_SCRIPT_EXTENSIONS = (".s", ".txt")
DEFAULT_CONFLICT_POLICY = "preserve"
CONFLICT_POLICIES = {"preserve", "precedence"}
EXPORTABLE_ITEM_STATUSES = {"selected", "preserved"}
MAX_EXPORT_FILES = 5_000
MAX_EXPORT_BYTES = 256 * 1024 * 1024
COMMIT_RENAME_ATTEMPTS = 6
COMMIT_RENAME_DELAY_SECONDS = 0.05
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
INVALID_WINDOWS_CHARS = set('<>:"|?*')


def _commit_staging_directory(staging_path: Path, output_path: Path) -> None:
    """Rename a completed staging tree, tolerating short-lived Windows locks."""

    for attempt in range(COMMIT_RENAME_ATTEMPTS):
        try:
            os.rename(staging_path, output_path)
            return
        except PermissionError:
            if attempt + 1 == COMMIT_RENAME_ATTEMPTS:
                raise
            time.sleep(COMMIT_RENAME_DELAY_SECONDS * (2**attempt))


class ExportPlanError(ValueError):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _natural_key(path):
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(path).replace("\\", "/"))
    )


def _is_within(path, parent):
    path_text = os.path.normcase(str(Path(path).resolve()))
    parent_text = os.path.normcase(str(Path(parent).resolve()))
    try:
        return os.path.commonpath([path_text, parent_text]) == parent_text
    except ValueError:
        return False


def _normalize_extensions(extensions):
    values = extensions or DEFAULT_SCRIPT_EXTENSIONS
    normalized = []
    for value in values:
        extension = str(value).strip().casefold()
        if not extension:
            raise ExportPlanError("invalid_input", "script extension cannot be empty")
        if not extension.startswith("."):
            extension = "." + extension
        if any(char in extension for char in "/\\:*?\"<>|"):
            raise ExportPlanError("invalid_input", f"invalid script extension: {value}")
        normalized.append(extension)
    return tuple(sorted(set(normalized)))


def _normalize_conflict_policy(conflict_policy):
    policy = str(conflict_policy or DEFAULT_CONFLICT_POLICY).strip().casefold()
    if policy not in CONFLICT_POLICIES:
        choices = ", ".join(sorted(CONFLICT_POLICIES))
        raise ExportPlanError(
            "invalid_input",
            f"unknown conflict policy: {conflict_policy}; expected one of {choices}",
        )
    return policy


def _safe_output_path(internal_path):
    if not internal_path or "\x00" in internal_path:
        raise ExportPlanError("unsafe_path", "internal path is empty or contains NUL")
    windows_path = PureWindowsPath(internal_path)
    if windows_path.is_absolute() or windows_path.drive or windows_path.root:
        raise ExportPlanError("unsafe_path", "absolute internal paths are not exportable")
    normalized = internal_path.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ExportPlanError("unsafe_path", "internal path contains an empty or traversal component")
    for part in parts:
        if any(ord(char) < 32 or char in INVALID_WINDOWS_CHARS for char in part):
            raise ExportPlanError("unsafe_path", "internal path contains a Windows-invalid character")
        if part.endswith((" ", ".")):
            raise ExportPlanError("unsafe_path", "internal path component ends with a space or dot")
        basename = part.split(".", 1)[0].upper()
        if basename in WINDOWS_RESERVED_NAMES:
            raise ExportPlanError("unsafe_path", "internal path uses a reserved Windows filename")
    output_path = "/".join(parts)
    if len(output_path) > 240:
        raise ExportPlanError("unsafe_path", "internal path exceeds the export path safety limit")
    return output_path


def _empty_plan(
    game_dir,
    output_dir,
    status,
    reason,
    extensions=(),
    conflict_policy=DEFAULT_CONFLICT_POLICY,
    warnings=(),
):
    return QlieScriptExportPlan(
        schema_version=REPORT_SCHEMA_VERSION,
        engine="qlie",
        game_dir=str(game_dir),
        output_dir=str(output_dir),
        status=status,
        reason=reason,
        archive_order=(),
        archive_order_explicit=False,
        conflict_policy=conflict_policy,
        extensions=tuple(extensions),
        warnings=tuple(warnings),
    )


def _resolve_archives(game_dir, archives):
    explicit = bool(archives)
    if explicit:
        paths = []
        seen = set()
        for value in archives:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = game_dir / path
            path = path.resolve()
            if not _is_within(path, game_dir):
                raise ExportPlanError("invalid_input", f"archive is outside the game directory: {path}")
            key = os.path.normcase(str(path))
            if key in seen:
                raise ExportPlanError("invalid_input", f"archive appears more than once: {path}")
            if not path.is_file():
                raise ExportPlanError("invalid_input", f"archive is not a regular file: {path}")
            seen.add(key)
            paths.append(path)
        return tuple(paths), True

    game_data_roots = [
        path
        for path in game_dir.iterdir()
        if path.is_dir() and path.name.casefold() == "gamedata"
    ]
    game_data_paths = [
        path.resolve()
        for root in game_data_roots
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".pack"
    ]
    candidates = game_data_paths or [
        path.resolve()
        for path in game_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".pack"
    ]
    paths = sorted(
        candidates,
        key=lambda path: _natural_key(path.relative_to(game_dir)),
    )
    return tuple(paths), False


def build_script_export_plan(
    game_dir,
    output_dir,
    *,
    archives=None,
    extensions=None,
    conflict_policy=DEFAULT_CONFLICT_POLICY,
    max_files=MAX_EXPORT_FILES,
    max_decoded_bytes=MAX_EXPORT_BYTES,
):
    """Build a metadata-only export plan; no entry payloads are read."""

    game_path = Path(game_dir).expanduser().resolve()
    output_path = Path(output_dir).expanduser().resolve()
    try:
        normalized_policy = _normalize_conflict_policy(conflict_policy)
        normalized_extensions = _normalize_extensions(extensions)
        if not game_path.exists() or not game_path.is_dir():
            raise ExportPlanError("invalid_input", f"game directory is not readable: {game_path}")
        if output_path.parent == output_path:
            raise ExportPlanError("invalid_input", "filesystem root cannot be used as an export directory")
        if _is_within(output_path, game_path) or _is_within(game_path, output_path):
            raise ExportPlanError("unsafe_output", "export directory must not overlap the game directory")
        archive_paths, explicit_order = _resolve_archives(game_path, archives)
        if not archive_paths:
            raise ExportPlanError("no_candidates", "no .pack archives were selected")
    except ExportPlanError as exc:
        return _empty_plan(
            game_path,
            output_path,
            exc.status,
            exc.reason,
            locals().get("normalized_extensions", ()),
            locals().get("normalized_policy", DEFAULT_CONFLICT_POLICY),
        )

    items = []
    warnings = []
    archive_errors = False
    archive_names = tuple(path.relative_to(game_path).as_posix() for path in archive_paths)
    for archive_rank, (archive_path, archive_name) in enumerate(zip(archive_paths, archive_names)):
        toc_report = inspect_filepack_toc(archive_path)
        if toc_report.status not in {"supported", "partial"}:
            archive_errors = True
            warnings.append(f"{archive_name}: {toc_report.status}: {toc_report.reason}")
            continue
        warnings.extend(f"{archive_name}: {warning}" for warning in toc_report.warnings)
        for entry in toc_report.entries:
            suffix = PureWindowsPath(entry.internal_path).suffix.casefold()
            if suffix not in normalized_extensions:
                continue
            try:
                relative_output = _safe_output_path(entry.internal_path)
                item_status = "candidate"
                item_reason = "allowlisted script candidate"
            except ExportPlanError as exc:
                relative_output = None
                item_status = "rejected"
                item_reason = exc.reason
            items.append(
                QlieScriptExportItem(
                    archive_path=str(archive_path),
                    archive_name=archive_name,
                    archive_rank=archive_rank,
                    entry_index=entry.index,
                    internal_path=entry.internal_path,
                    output_path=relative_output,
                    status=item_status,
                    reason=item_reason,
                    stored_size=entry.stored_size,
                    original_size=entry.original_size,
                    compression_flag=entry.compression_flag,
                    obfuscation_flag=entry.obfuscation_flag,
                    entry_hash=entry.entry_hash,
                )
            )

    groups = {}
    for index, item in enumerate(items):
        if item.status == "candidate":
            groups.setdefault(item.output_path.casefold(), []).append(index)
    for group in groups.values():
        if len(group) == 1:
            index = group[0]
            item = items[index]
            if normalized_policy == "preserve":
                try:
                    resolved_path = _safe_output_path(f"resolved/{item.output_path}")
                    items[index] = replace(
                        item,
                        output_path=resolved_path,
                        status="selected",
                        reason="unique logical path placed in resolved view",
                    )
                except ExportPlanError as exc:
                    items[index] = replace(
                        item,
                        output_path=None,
                        status="rejected",
                        reason=exc.reason,
                    )
            else:
                items[index] = replace(item, status="selected", reason="unique output path")
            continue
        archive_ranks = [items[index].archive_rank for index in group]
        if normalized_policy == "preserve":
            logical_path = items[group[0]].output_path
            rank_counts = {
                rank: archive_ranks.count(rank) for rank in set(archive_ranks)
            }
            for index in group:
                item = items[index]
                entry_layer = (
                    f"entry-{item.entry_index}/" if rank_counts[item.archive_rank] > 1 else ""
                )
                try:
                    layer_path = _safe_output_path(
                        f"layers/{item.archive_name}/{entry_layer}{item.output_path}"
                    )
                    items[index] = replace(
                        item,
                        output_path=layer_path,
                        status="preserved",
                        reason="duplicate logical path preserved in its archive layer",
                        conflict_group=logical_path,
                    )
                except ExportPlanError as exc:
                    items[index] = replace(
                        item,
                        output_path=None,
                        status="rejected",
                        reason=exc.reason,
                        conflict_group=logical_path,
                    )
        elif explicit_order and len(set(archive_ranks)) == len(archive_ranks):
            winner_index = max(group, key=lambda index: items[index].archive_rank)
            winner = items[winner_index]
            logical_path = winner.output_path
            for index in group:
                if index == winner_index:
                    items[index] = replace(
                        items[index],
                        status="selected",
                        reason="selected by explicit archive precedence",
                        conflict_group=logical_path,
                    )
                else:
                    items[index] = replace(
                        items[index],
                        status="shadowed",
                        reason="shadowed by later archive in explicit order",
                        shadowed_by=f"{winner.archive_name}#{winner.entry_index}",
                        conflict_group=logical_path,
                    )
        else:
            logical_path = items[group[0]].output_path
            for index in group:
                items[index] = replace(
                    items[index],
                    status="conflict",
                    reason="duplicate output path requires explicit archive precedence",
                    conflict_group=logical_path,
                )

    generated_paths = {}
    for index, item in enumerate(items):
        if item.status in EXPORTABLE_ITEM_STATUSES:
            generated_paths.setdefault(item.output_path.casefold(), []).append(index)
    for indexes in generated_paths.values():
        if len(indexes) > 1:
            for index in indexes:
                items[index] = replace(
                    items[index],
                    output_path=None,
                    status="rejected",
                    reason="generated export path collides after normalization",
                )

    selected = [item for item in items if item.status in EXPORTABLE_ITEM_STATUSES]
    rejected_count = sum(item.status == "rejected" for item in items)
    conflict_count = sum(item.status == "conflict" for item in items)
    estimated_bytes = sum(item.original_size for item in selected)
    if archive_errors:
        status = "blocked"
        reason = "one or more selected archives could not be parsed"
    elif rejected_count:
        status = "blocked"
        reason = "one or more allowlisted entries have unsafe output paths"
    elif conflict_count:
        status = "needs_precedence"
        reason = "duplicate script paths require an explicit low-to-high archive order"
    elif not selected:
        status = "no_candidates"
        reason = "no allowlisted script entries were found"
    elif len(selected) > max_files:
        status = "limit_exceeded"
        reason = f"selected file count exceeds safety limit: {len(selected)} > {max_files}"
    elif estimated_bytes > max_decoded_bytes:
        status = "limit_exceeded"
        reason = (
            f"estimated decoded size exceeds safety limit: "
            f"{estimated_bytes} > {max_decoded_bytes}"
        )
    else:
        status = "ready"
        reason = (
            "export plan preserves duplicate variants in archive layers"
            if normalized_policy == "preserve"
            else "export plan passed path, precedence, and resource checks"
        )

    return QlieScriptExportPlan(
        schema_version=REPORT_SCHEMA_VERSION,
        engine="qlie",
        game_dir=str(game_path),
        output_dir=str(output_path),
        status=status,
        reason=reason,
        archive_order=archive_names,
        archive_order_explicit=explicit_order,
        conflict_policy=normalized_policy,
        extensions=normalized_extensions,
        items=tuple(items),
        warnings=tuple(warnings),
    )


def _export_result(plan, status, reason, items=None, manifest_path=None, warnings=None):
    return QlieScriptExportResult(
        schema_version=REPORT_SCHEMA_VERSION,
        engine="qlie",
        status=status,
        reason=reason,
        output_dir=plan.output_dir,
        manifest_path=manifest_path,
        items=tuple(plan.items if items is None else items),
        warnings=tuple(plan.warnings if warnings is None else warnings),
    )


def apply_script_export_plan(
    plan,
    *,
    exe_path,
    key_file_path=None,
    max_decoded_bytes=MAX_EXPORT_BYTES,
):
    """Decode all selected scripts first, then atomically publish a new output directory."""

    if plan.status != "ready":
        return _export_result(plan, "blocked", f"export plan is not ready: {plan.reason}")
    output_path = Path(plan.output_dir)
    if output_path.exists():
        return _export_result(plan, "output_exists", "export directory already exists; overwrite is disabled")

    decoded_items = []
    payloads = []
    total_decoded = 0
    for item in plan.items:
        if item.status not in EXPORTABLE_ITEM_STATUSES:
            decoded_items.append(item)
            continue
        result = read_filepack_entry(
            item.archive_path,
            entry_index=item.entry_index,
            exe_path=exe_path,
            key_file_path=key_file_path,
        )
        if result.report.status != "supported" or result.data is None:
            failed_item = replace(
                item,
                status="decode_failed",
                reason=result.report.reason,
                stored_sha256=result.report.stored_sha256,
            )
            decoded_items.append(failed_item)
            decoded_items.extend(plan.items[len(decoded_items) :])
            return _export_result(
                plan,
                "decode_failed",
                f"failed to decode {item.archive_name}#{item.entry_index}: {result.report.reason}",
                items=decoded_items,
            )
        total_decoded += len(result.data)
        if total_decoded > max_decoded_bytes:
            return _export_result(
                plan,
                "limit_exceeded",
                "decoded script total exceeds export safety limit",
                items=decoded_items + [item],
            )
        exported_item = replace(
            item,
            status="exported",
            reason="decoded and staged for transactional export",
            stored_sha256=result.report.stored_sha256,
            decoded_sha256=result.report.decoded_sha256,
            decoded_size=len(result.data),
        )
        decoded_items.append(exported_item)
        payloads.append((exported_item, result.data))

    manifest = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "engine": "qlie",
        "game_dir": plan.game_dir,
        "output_dir": plan.output_dir,
        "archive_order": list(plan.archive_order),
        "archive_order_explicit": plan.archive_order_explicit,
        "conflict_policy": plan.conflict_policy,
        "extensions": list(plan.extensions),
        "summary": {
            "exported_count": len(payloads),
            "decoded_bytes": total_decoded,
        },
        "items": [item.to_dict() for item in decoded_items],
        "warnings": list(plan.warnings),
    }

    staging_path = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            return _export_result(
                plan,
                "output_exists",
                "export directory appeared during preflight; overwrite is disabled",
                items=decoded_items,
            )
        staging_path = Path(
            tempfile.mkdtemp(
                prefix=f".{output_path.name}.staging-",
                dir=str(output_path.parent),
            )
        )
        for item, data in payloads:
            target = staging_path.joinpath(*PurePosixPath(item.output_path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        manifest_target = staging_path / "manifest.json"
        with manifest_target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _commit_staging_directory(staging_path, output_path)
        staging_path = None
    except OSError as exc:
        return _export_result(
            plan,
            "write_failed",
            f"transactional export failed: {exc}",
            items=decoded_items,
        )
    finally:
        if staging_path is not None and staging_path.exists():
            shutil.rmtree(staging_path, ignore_errors=True)

    return _export_result(
        plan,
        "exported",
        "scripts exported transactionally to a new directory",
        items=decoded_items,
        manifest_path=str(output_path / "manifest.json"),
    )


def render_export_plan_text(plan, limit=50):
    summary = plan.to_dict()["summary"]
    lines = [
        f"QLIE script export plan: {plan.status}",
        f"game_dir: {plan.game_dir}",
        f"output_dir: {plan.output_dir}",
        f"archive_order: {' -> '.join(plan.archive_order) or 'none'}",
        f"archive_order_explicit: {plan.archive_order_explicit}",
        f"conflict_policy: {plan.conflict_policy}",
        f"extensions: {', '.join(plan.extensions)}",
        (
            f"items: {summary['item_count']} exportable={summary['selected_count']} "
            f"resolved={summary['resolved_count']} preserved={summary['preserved_count']} "
            f"estimated_bytes={summary['estimated_decoded_bytes']}"
        ),
        f"reason: {plan.reason}",
    ]
    noteworthy = [item for item in plan.items if item.status not in {"selected", "exported"}]
    for item in noteworthy[:limit]:
        lines.append(
            f"- {item.status}: {item.archive_name}#{item.entry_index} "
            f"{item.internal_path} ({item.reason})"
        )
    if len(noteworthy) > limit:
        lines.append(f"... {len(noteworthy) - limit} non-selected items omitted; use --json")
    lines.extend(f"warning: {warning}" for warning in plan.warnings)
    return "\n".join(lines)


def render_export_result_text(result):
    summary = result.to_dict()["summary"]
    lines = [
        f"QLIE script export: {result.status}",
        f"output_dir: {result.output_dir}",
        f"exported: {summary['exported_count']} files, {summary['decoded_bytes']} bytes",
        f"manifest: {result.manifest_path or 'not written'}",
        f"reason: {result.reason}",
    ]
    lines.extend(f"warning: {warning}" for warning in result.warnings)
    return "\n".join(lines)
