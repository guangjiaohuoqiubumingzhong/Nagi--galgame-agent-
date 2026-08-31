"""Translation diff, preview, and guarded sidecar application.

Previews remain content-free unless text is explicitly requested.  Applying a
contentful preview defaults to a disposable dry run and may only publish a new
sidecar script tree; source scripts and game archives are never modified.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .models import TranslationCandidate, TranslationUnit
from .terminology import TerminologySnapshot
from .validator import TranslationBatchValidationReport, validate_translation_candidates

TRANSLATION_PREVIEW_VERSION = 1
TRANSLATION_PREVIEW_ID_PREFIX = "tprev_v1_"


def _sha256(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(payload):
    return _sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


def _is_within(path, parent):
    path_text = os.path.normcase(str(Path(path).resolve()))
    parent_text = os.path.normcase(str(Path(parent).resolve()))
    try:
        return os.path.commonpath([path_text, parent_text]) == parent_text
    except ValueError:
        return False


def _git_root_for(path):
    candidate = Path(path).resolve()
    if not candidate.is_dir():
        candidate = candidate.parent
    for current in (candidate, *candidate.parents):
        if (current / ".git").exists():
            return current
    return None


def _validate_digest(value, field_name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


class TranslationPreviewError(ValueError):
    """Fail-closed error raised before an unsafe preview is generated."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

    def to_dict(self):
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class TranslationPreviewSpec:
    """Identity of the run/corpus a preview is allowed to describe."""

    run_id: str
    plan_id: str
    corpus_dir: str
    segments_sha256: str
    parse_report_sha256: str

    def __post_init__(self):
        for value, field_name in (
            (self.run_id, "run_id"),
            (self.plan_id, "plan_id"),
            (self.corpus_dir, "corpus_dir"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        _validate_digest(self.segments_sha256, "segments_sha256")
        _validate_digest(self.parse_report_sha256, "parse_report_sha256")

    def identity_dict(self):
        return {
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "corpus_dir": str(Path(self.corpus_dir).expanduser().resolve()),
            "segments_sha256": self.segments_sha256,
            "parse_report_sha256": self.parse_report_sha256,
        }


@dataclass(frozen=True)
class TranslationDiff:
    """One source-to-candidate change with optional in-memory diff text."""

    unit_id: str
    segment_id: str
    kind: str
    speaker_sha256: str | None
    output_path: str
    archive_name: str
    internal_path: str
    encoding: str
    source_sha256: str
    byte_start: int
    byte_end: int
    source_text_sha256: str
    translated_text_sha256: str
    source_text: str = field(repr=False)
    translated_text: str = field(repr=False)

    def to_dict(self, *, include_text=False):
        payload = {
            "unit_id": self.unit_id,
            "segment_id": self.segment_id,
            "kind": self.kind,
            "speaker_sha256": self.speaker_sha256,
            "output_path": self.output_path,
            "archive_name": self.archive_name,
            "internal_path": self.internal_path,
            "encoding": self.encoding,
            "source_sha256": self.source_sha256,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "source_text_sha256": self.source_text_sha256,
            "translated_text_sha256": self.translated_text_sha256,
        }
        if include_text:
            payload["source_text"] = self.source_text
            payload["translated_text"] = self.translated_text
            payload["unified_diff"] = "".join(
                difflib.unified_diff(
                    self.source_text.splitlines(keepends=True),
                    self.translated_text.splitlines(keepends=True),
                    fromfile=f"source/{self.unit_id}",
                    tofile=f"candidate/{self.unit_id}",
                    lineterm="\n",
                )
            )
        return payload


@dataclass(frozen=True)
class TranslationPreviewReport:
    """Preview report; default serialization contains no source or translation prose."""

    preview_id: str
    spec: TranslationPreviewSpec
    status: str
    patch_eligible: bool
    validation: TranslationBatchValidationReport
    diffs: tuple[TranslationDiff, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if self.status not in {"valid", "needs_review", "invalid"}:
            raise ValueError("unsupported preview status")
        if not isinstance(self.validation, TranslationBatchValidationReport):
            raise TypeError("validation must be a TranslationBatchValidationReport")
        if self.patch_eligible and self.status == "invalid":
            raise ValueError("invalid preview cannot be patch eligible")
        if any(not isinstance(item, TranslationDiff) for item in self.diffs):
            raise TypeError("diffs must contain TranslationDiff values")

    def to_dict(self, *, include_text=False):
        return {
            "preview_version": TRANSLATION_PREVIEW_VERSION,
            "preview_id": self.preview_id,
            "spec": self.spec.identity_dict(),
            "status": self.status,
            "patch_eligible": self.patch_eligible,
            "summary": {
                "diff_count": len(self.diffs),
                "unit_count": len(self.validation.reports),
                "issue_count": self.validation.issue_count,
                "expected_token_count": self.validation.expected_token_count,
                "preserved_token_count": self.validation.preserved_token_count,
            },
            "validation": self.validation.to_dict(),
            "diffs": [item.to_dict(include_text=include_text) for item in self.diffs],
        }

    def to_json(self, *, include_text=False):
        return (
            json.dumps(
                self.to_dict(include_text=include_text),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


@dataclass(frozen=True)
class TranslationPreviewPublishResult:
    output_dir: str
    preview_path: str
    manifest_path: str
    preview_sha256: str

    def to_dict(self):
        return {
            "output_dir": self.output_dir,
            "preview_path": self.preview_path,
            "manifest_path": self.manifest_path,
            "preview_sha256": self.preview_sha256,
        }


@dataclass(frozen=True)
class TranslationApplyResult:
    status: str
    preview_id: str
    changed_file_count: int
    changed_unit_count: int
    output_dir: str | None = None
    reason: str = ""

    def __post_init__(self):
        if self.status not in {"dry_run", "applied"}:
            raise ValueError("unsupported translation apply status")

    def to_dict(self):
        return {
            "status": self.status,
            "preview_id": self.preview_id,
            "changed_file_count": self.changed_file_count,
            "changed_unit_count": self.changed_unit_count,
            "output_dir": self.output_dir,
            "reason": self.reason,
        }


def _check_current_identity(
    units,
    candidates,
    spec,
    *,
    current_segments_sha256,
    current_parse_report_sha256,
    current_source_hashes,
    expected_candidate_hashes,
):
    if current_segments_sha256 != spec.segments_sha256:
        raise TranslationPreviewError(
            "segments_hash_mismatch",
            "current segments hash does not match preview spec",
        )
    if current_parse_report_sha256 != spec.parse_report_sha256:
        raise TranslationPreviewError(
            "parse_report_hash_mismatch",
            "current parse report hash does not match preview spec",
        )
    if not isinstance(current_source_hashes, Mapping):
        raise TypeError("current_source_hashes must be a mapping")
    unit_ids = {unit.unit_id for unit in units}
    if set(current_source_hashes) != unit_ids:
        raise TranslationPreviewError(
            "source_hash_set_mismatch", "current source hash set does not match units"
        )
    for unit in units:
        if current_source_hashes[unit.unit_id] != unit.source.source_sha256:
            raise TranslationPreviewError(
                "source_hash_mismatch",
                "current source file hash does not match unit source",
            )
        _validate_digest(current_source_hashes[unit.unit_id], "current_source_hash")

    if expected_candidate_hashes is not None:
        if not isinstance(expected_candidate_hashes, Mapping):
            raise TypeError("expected_candidate_hashes must be a mapping or None")
        if set(expected_candidate_hashes) != {
            candidate.unit_id for candidate in candidates
        }:
            raise TranslationPreviewError(
                "candidate_hash_set_mismatch",
                "candidate hash set does not match candidates",
            )
        for candidate in candidates:
            if (
                expected_candidate_hashes[candidate.unit_id]
                != candidate.translated_text_sha256
            ):
                raise TranslationPreviewError(
                    "candidate_hash_mismatch",
                    "candidate hash does not match expected hash",
                )
            _validate_digest(
                expected_candidate_hashes[candidate.unit_id], "expected_candidate_hash"
            )


def build_translation_patch_preview(
    units,
    candidates,
    spec: TranslationPreviewSpec,
    *,
    current_segments_sha256,
    current_parse_report_sha256,
    current_source_hashes,
    terminology_snapshot: TerminologySnapshot,
    expected_candidate_hashes=None,
    max_line_chars=None,
) -> TranslationPreviewReport:
    """Build a diff preview only after identity and quality checks pass."""

    if not isinstance(spec, TranslationPreviewSpec):
        raise TypeError("spec must be a TranslationPreviewSpec")
    if not isinstance(terminology_snapshot, TerminologySnapshot):
        raise TypeError("terminology_snapshot must be a TerminologySnapshot")
    units = tuple(units)
    candidates = tuple(candidates)
    if any(not isinstance(unit, TranslationUnit) for unit in units):
        raise TypeError("units must contain TranslationUnit values")
    if any(not isinstance(candidate, TranslationCandidate) for candidate in candidates):
        raise TypeError("candidates must contain TranslationCandidate values")
    _check_current_identity(
        units,
        candidates,
        spec,
        current_segments_sha256=current_segments_sha256,
        current_parse_report_sha256=current_parse_report_sha256,
        current_source_hashes=current_source_hashes,
        expected_candidate_hashes=expected_candidate_hashes,
    )

    validation = validate_translation_candidates(
        units,
        candidates,
        max_line_chars=max_line_chars,
        terminology_snapshot=terminology_snapshot,
    )
    candidate_by_id = {candidate.unit_id: candidate for candidate in candidates}
    unit_by_id = {unit.unit_id: unit for unit in units}
    diffs = []
    if validation.status != "invalid":
        for report in validation.reports:
            unit = unit_by_id.get(report.unit_id)
            candidate = candidate_by_id.get(report.unit_id)
            if (
                unit is None
                or candidate is None
                or unit.source_text == candidate.translated_text
            ):
                continue
            source = unit.source
            diffs.append(
                TranslationDiff(
                    unit_id=unit.unit_id,
                    segment_id=unit.segment_id,
                    kind=unit.kind,
                    speaker_sha256=_sha256(unit.speaker)
                    if unit.speaker is not None
                    else None,
                    output_path=source.output_path,
                    archive_name=source.archive_name,
                    internal_path=source.internal_path,
                    encoding=source.encoding,
                    source_sha256=source.source_sha256,
                    byte_start=source.byte_start,
                    byte_end=source.byte_end,
                    source_text_sha256=unit.source_text_sha256,
                    translated_text_sha256=candidate.translated_text_sha256,
                    source_text=unit.source_text,
                    translated_text=candidate.translated_text,
                )
            )

    preview_id = TRANSLATION_PREVIEW_ID_PREFIX + _canonical_sha256(
        {
            "preview_version": TRANSLATION_PREVIEW_VERSION,
            "spec": spec.identity_dict(),
            "terminology_version": terminology_snapshot.version,
            "terminology_snapshot_sha256": terminology_snapshot.snapshot_sha256,
            "candidate_ids": [candidate.candidate_id for candidate in candidates],
            "candidate_hashes": [
                candidate.translated_text_sha256 for candidate in candidates
            ],
            "validation": validation.to_dict(),
        }
    )
    return TranslationPreviewReport(
        preview_id=preview_id,
        spec=spec,
        status=validation.status,
        patch_eligible=validation.patch_eligible,
        validation=validation,
        diffs=tuple(diffs),
    )


def _resolve_preview_output(output_dir, corpus_dir, forbidden_roots):
    output = Path(output_dir).expanduser().absolute()
    if output.parent == output:
        raise TranslationPreviewError(
            "unsafe_output", "filesystem root cannot be used for preview output"
        )
    for current in (output, *output.parents):
        if current.exists() and current.is_symlink():
            raise TranslationPreviewError(
                "unsafe_output", "preview output must not traverse symbolic links"
            )
    output = output.resolve()
    corpus = Path(corpus_dir).expanduser().resolve()
    if _is_within(output, corpus) or _is_within(corpus, output):
        raise TranslationPreviewError(
            "unsafe_output", "preview output must not overlap the source corpus"
        )
    git_root = _git_root_for(output)
    if git_root is not None:
        raise TranslationPreviewError(
            "unsafe_output", "preview output must be outside a Git worktree"
        )
    for root in forbidden_roots:
        root = Path(root).expanduser().resolve()
        if _is_within(output, root) or _is_within(root, output):
            raise TranslationPreviewError(
                "unsafe_output", "preview output overlaps a forbidden root"
            )
    if output.exists():
        raise TranslationPreviewError("output_exists", "preview output already exists")
    return output


def publish_translation_preview(
    report: TranslationPreviewReport,
    output_dir,
    *,
    include_text=False,
    forbidden_roots=(),
) -> TranslationPreviewPublishResult:
    """Publish a sidecar preview atomically; never writes game files."""

    if not isinstance(report, TranslationPreviewReport):
        raise TypeError("report must be a TranslationPreviewReport")
    if not report.patch_eligible:
        raise TranslationPreviewError(
            "quality_gate_failed", "preview is not eligible for publishing"
        )
    output = _resolve_preview_output(
        output_dir, report.spec.corpus_dir, forbidden_roots
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise TranslationPreviewError("output_exists", "preview output already exists")

    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=str(output.parent))
    )
    try:
        preview_path = staging / "preview.json"
        preview_text = report.to_json(include_text=include_text)
        with preview_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(preview_text)
        manifest_payload = {
            "preview_version": TRANSLATION_PREVIEW_VERSION,
            "preview_id": report.preview_id,
            "preview_sha256": _sha256(preview_text),
            "status": report.status,
            "patch_eligible": report.patch_eligible,
            "content_included": bool(include_text),
        }
        manifest_path = staging / "manifest.json"
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                manifest_payload, stream, ensure_ascii=False, indent=2, sort_keys=True
            )
            stream.write("\n")
        try:
            os.rename(staging, output)
        except FileExistsError as exc:
            raise TranslationPreviewError(
                "output_exists", "preview output already exists"
            ) from exc
        staging = None
    finally:
        if staging is not None and staging.exists():
            for path in staging.iterdir():
                path.unlink()
            staging.rmdir()

    return TranslationPreviewPublishResult(
        output_dir=str(output),
        preview_path=str(output / "preview.json"),
        manifest_path=str(output / "manifest.json"),
        preview_sha256=_sha256(preview_text),
    )


_APPLY_PREVIEW_MAX_BYTES = 256 * 1024 * 1024
_APPLY_MANIFEST_MAX_BYTES = 1024 * 1024
_CODECS = {
    "cp932": "cp932",
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "utf-16-le": "utf-16-le",
    "utf-16-le-bom": "utf-16",
    "utf-16-be": "utf-16-be",
    "utf-16-be-bom": "utf-16",
}


def _read_bounded_regular_file(path, *, max_bytes, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise TranslationPreviewError(
            "invalid_preview", f"{label} is missing or unsafe"
        )
    try:
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise TranslationPreviewError(
                "invalid_preview", f"{label} exceeds the accepted size"
            )
        content = path.read_bytes()
    except TranslationPreviewError:
        raise
    except OSError as exc:
        raise TranslationPreviewError(
            "invalid_preview", f"{label} cannot be read safely"
        ) from exc
    if len(content) != size or len(content) > max_bytes:
        raise TranslationPreviewError(
            "invalid_preview", f"{label} exceeds the accepted size"
        )
    return content


def _parse_json_object(content, *, label):

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TranslationPreviewError(
                    "invalid_preview", f"{label} contains duplicate JSON keys"
                )
            result[key] = value
        return result

    try:
        payload = json.loads(
            content.decode("utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except TranslationPreviewError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TranslationPreviewError(
            "invalid_preview", f"{label} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise TranslationPreviewError(
            "invalid_preview", f"{label} must be a JSON object"
        )
    return payload


def _load_json_object(path, *, max_bytes, label):
    content = _read_bounded_regular_file(path, max_bytes=max_bytes, label=label)
    return _parse_json_object(content, label=label)


def _resolve_safe_existing_directory(value, *, code, label):
    try:
        raw = Path(value).expanduser().absolute()
    except (TypeError, ValueError, OSError) as exc:
        raise TranslationPreviewError(code, f"{label} is missing or unsafe") from exc
    for current in (raw, *raw.parents):
        if current.exists() and current.is_symlink():
            raise TranslationPreviewError(
                code, f"{label} must not traverse symbolic links"
            )
    if not raw.is_dir():
        raise TranslationPreviewError(code, f"{label} is missing or unsafe")
    try:
        return raw.resolve(strict=True)
    except OSError as exc:
        raise TranslationPreviewError(code, f"{label} is missing or unsafe") from exc


def _safe_apply_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise TranslationPreviewError(
            "unsafe_path", "preview output_path must be a relative POSIX path"
        )
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ":" in relative.parts[0]
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise TranslationPreviewError(
            "unsafe_path", "preview output_path contains traversal"
        )
    return relative


def _verify_published_preview(preview_dir):
    preview_dir = _resolve_safe_existing_directory(
        preview_dir, code="invalid_preview", label="preview directory"
    )
    preview_path = preview_dir / "preview.json"
    manifest_path = preview_dir / "manifest.json"
    manifest = _load_json_object(
        manifest_path, max_bytes=_APPLY_MANIFEST_MAX_BYTES, label="preview manifest"
    )
    preview_bytes = _read_bounded_regular_file(
        preview_path, max_bytes=_APPLY_PREVIEW_MAX_BYTES, label="preview"
    )
    expected_hash = manifest.get("preview_sha256")
    if not isinstance(expected_hash, str) or _sha256(preview_bytes) != expected_hash:
        raise TranslationPreviewError(
            "preview_hash_mismatch", "preview content does not match its manifest"
        )
    if manifest.get("content_included") is not True:
        raise TranslationPreviewError(
            "preview_content_required", "applying a content-free preview is forbidden"
        )
    preview = _parse_json_object(preview_bytes, label="preview")
    if preview.get("preview_version") != TRANSLATION_PREVIEW_VERSION:
        raise TranslationPreviewError("invalid_preview", "unsupported preview version")
    if preview.get("preview_id") != manifest.get("preview_id"):
        raise TranslationPreviewError(
            "preview_identity_mismatch", "preview ID does not match its manifest"
        )
    if preview.get("patch_eligible") is not True or preview.get("status") == "invalid":
        raise TranslationPreviewError(
            "quality_gate_failed", "preview is not eligible for applying"
        )
    spec = preview.get("spec")
    diffs = preview.get("diffs")
    if not isinstance(spec, dict) or not isinstance(diffs, list):
        raise TranslationPreviewError(
            "invalid_preview", "preview spec or diffs are invalid"
        )
    required_spec = {
        "run_id",
        "plan_id",
        "corpus_dir",
        "segments_sha256",
        "parse_report_sha256",
    }
    if set(spec) != required_spec:
        raise TranslationPreviewError(
            "invalid_preview", "preview spec fields are invalid"
        )
    _validate_digest(spec["segments_sha256"], "preview segments hash")
    _validate_digest(spec["parse_report_sha256"], "preview parse report hash")
    return preview_dir, preview, spec, diffs, preview_bytes


def _verify_current_corpus(spec):
    corpus_dir = _resolve_safe_existing_directory(
        spec["corpus_dir"], code="corpus_missing", label="current corpus directory"
    )
    segments_path = corpus_dir / "segments.jsonl"
    report_path = corpus_dir / "parse-report.json"
    for path, label in (
        (segments_path, "current segments"),
        (report_path, "current parse report"),
    ):
        if path.is_symlink() or not path.is_file():
            raise TranslationPreviewError(
                "corpus_missing", f"{label} is missing or unsafe"
            )
    if _sha256(segments_path.read_bytes()) != spec["segments_sha256"]:
        raise TranslationPreviewError(
            "segments_hash_mismatch", "current segments hash does not match preview"
        )
    if _sha256(report_path.read_bytes()) != spec["parse_report_sha256"]:
        raise TranslationPreviewError(
            "parse_report_hash_mismatch",
            "current parse report hash does not match preview",
        )
    return corpus_dir


def _resolve_apply_target(source_root, target_root, corpus_dir, forbidden_roots):
    source = _resolve_safe_existing_directory(
        source_root, code="invalid_source", label="source script root"
    )
    for root, dirs, files in os.walk(source):
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                raise TranslationPreviewError(
                    "unsafe_source", "source script tree contains a symbolic link"
                )
    target = Path(target_root).expanduser().absolute()
    if target.parent == target:
        raise TranslationPreviewError(
            "unsafe_output", "filesystem root cannot be used as apply target"
        )
    for current in (target, *target.parents):
        if current.exists() and current.is_symlink():
            raise TranslationPreviewError(
                "unsafe_output", "apply target must not traverse symbolic links"
            )
    target = target.resolve()
    if _is_within(target, source) or _is_within(source, target):
        raise TranslationPreviewError(
            "unsafe_output", "apply target must not overlap source scripts"
        )
    if _is_within(target, corpus_dir) or _is_within(corpus_dir, target):
        raise TranslationPreviewError(
            "unsafe_output", "apply target must not overlap corpus"
        )
    git_root = _git_root_for(target)
    if git_root is not None:
        raise TranslationPreviewError(
            "unsafe_output", "apply target must be outside a Git worktree"
        )
    for root in forbidden_roots:
        root = Path(root).expanduser().resolve()
        if _is_within(target, root) or _is_within(root, target):
            raise TranslationPreviewError(
                "unsafe_output", "apply target overlaps a forbidden root"
            )
    if target.exists():
        raise TranslationPreviewError("output_exists", "apply target already exists")
    return source, target


def _decode_preview_encoding(value):
    if value not in _CODECS:
        raise TranslationPreviewError(
            "unsupported_encoding", "preview uses an unsupported source encoding"
        )
    return _CODECS[value]


def _decode_preview_slice_encoding(value):
    if value == "utf-16-le-bom":
        return "utf-16-le"
    if value == "utf-16-be-bom":
        return "utf-16-be"
    if value == "utf-8-sig":
        return "utf-8"
    return _decode_preview_encoding(value)


def _validate_byte_diffs(source_bytes, file_diffs, *, slice_codec):
    ordered = sorted(file_diffs, key=lambda item: item["byte_start"])
    previous_end = 0
    for diff in ordered:
        byte_start = diff["byte_start"]
        byte_end = diff["byte_end"]
        if (
            isinstance(byte_start, bool)
            or not isinstance(byte_start, int)
            or isinstance(byte_end, bool)
            or not isinstance(byte_end, int)
            or byte_start < previous_end
            or byte_start < 0
            or byte_end <= byte_start
        ):
            raise TranslationPreviewError(
                "invalid_preview", "preview byte ranges are invalid or overlapping"
            )
        if byte_end > len(source_bytes):
            raise TranslationPreviewError(
                "source_changed",
                "preview source text does not identify the recorded byte range",
            )
        try:
            actual_source_text = source_bytes[byte_start:byte_end].decode(slice_codec)
        except UnicodeError as exc:
            raise TranslationPreviewError(
                "source_decode_failed", "preview source range cannot be decoded"
            ) from exc
        if actual_source_text != diff["source_text"]:
            raise TranslationPreviewError(
                "source_changed",
                "preview source text does not identify the recorded byte range",
            )
        previous_end = byte_end
    return ordered


def _apply_byte_diffs(source_bytes, ordered_diffs, *, encoding, fallback_encoding):
    slice_codec = _decode_preview_slice_encoding(encoding)
    try:
        replacements = [
            (diff, diff["translated_text"].encode(slice_codec))
            for diff in ordered_diffs
        ]
    except UnicodeEncodeError:
        if fallback_encoding != "utf-16-le-bom":
            raise
        parts = []
        cursor = 0
        whole_codec = _decode_preview_encoding(encoding)
        for diff in ordered_diffs:
            unchanged = source_bytes[cursor : diff["byte_start"]]
            if unchanged:
                parts.append(
                    unchanged.decode(whole_codec if cursor == 0 else slice_codec)
                )
            parts.append(diff["translated_text"])
            cursor = diff["byte_end"]
        trailing = source_bytes[cursor:]
        if trailing:
            parts.append(trailing.decode(whole_codec if cursor == 0 else slice_codec))
        return b"\xff\xfe" + "".join(parts).encode("utf-16-le")

    output = source_bytes
    for diff, replacement in reversed(replacements):
        output = (
            output[: diff["byte_start"]]
            + replacement
            + output[diff["byte_end"] :]
        )
    return output


def _apply_diffs_to_tree(source_root, staging_root, diffs, *, fallback_encoding=None):
    if fallback_encoding not in {None, "utf-16-le-bom"}:
        raise TranslationPreviewError(
            "unsupported_encoding",
            "fallback encoding must be null or utf-16-le-bom",
        )
    grouped = {}
    for diff in diffs:
        if not isinstance(diff, dict):
            raise TranslationPreviewError(
                "invalid_preview", "preview diff entries must be objects"
            )
        required = {
            "unit_id",
            "segment_id",
            "kind",
            "output_path",
            "archive_name",
            "internal_path",
            "speaker_sha256",
            "byte_start",
            "byte_end",
            "encoding",
            "source_sha256",
            "source_text_sha256",
            "translated_text_sha256",
            "source_text",
            "translated_text",
            "unified_diff",
        }
        if set(diff) != required:
            raise TranslationPreviewError(
                "invalid_preview", "preview diff fields are invalid"
            )
        relative = _safe_apply_path(diff["output_path"])
        _validate_digest(diff["source_sha256"], "preview source hash")
        _validate_digest(diff["source_text_sha256"], "preview source text hash")
        _validate_digest(diff["translated_text_sha256"], "preview translated text hash")
        if _sha256(diff["source_text"]) != diff["source_text_sha256"]:
            raise TranslationPreviewError(
                "source_text_hash_mismatch", "preview source text hash is invalid"
            )
        if _sha256(diff["translated_text"]) != diff["translated_text_sha256"]:
            raise TranslationPreviewError(
                "candidate_hash_mismatch", "preview candidate text hash is invalid"
            )
        grouped.setdefault((relative.as_posix(), diff["encoding"]), []).append(diff)

    changed_files = 0
    for (relative_text, encoding), file_diffs in sorted(grouped.items()):
        source_path = source_root.joinpath(*PurePosixPath(relative_text).parts)
        target_path = staging_root.joinpath(*PurePosixPath(relative_text).parts)
        if source_path.is_symlink() or not source_path.is_file():
            raise TranslationPreviewError(
                "source_missing", "preview source script is missing or unsafe"
            )
        try:
            source_bytes = source_path.read_bytes()
        except OSError as exc:
            raise TranslationPreviewError(
                "source_read_failed", "preview source script cannot be read"
            ) from exc
        slice_codec = _decode_preview_slice_encoding(encoding)
        ordered_diffs = _validate_byte_diffs(
            source_bytes,
            file_diffs,
            slice_codec=slice_codec,
        )
        try:
            encoded = _apply_byte_diffs(
                source_bytes,
                ordered_diffs,
                encoding=encoding,
                fallback_encoding=fallback_encoding,
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(encoded)
        except (OSError, UnicodeError) as exc:
            raise TranslationPreviewError(
                "apply_write_failed", "staging script write failed"
            ) from exc
        changed_files += 1
    return changed_files


def apply_translation_preview(
    preview_dir,
    source_root,
    target_root,
    *,
    approved=False,
    dry_run=True,
    forbidden_roots=(),
    fallback_encoding=None,
) -> TranslationApplyResult:
    """Apply a published preview to a new sidecar tree, never to the source tree."""

    if not isinstance(dry_run, bool) or not isinstance(approved, bool):
        raise TypeError("approved and dry_run must be booleans")
    if not dry_run and not approved:
        raise TranslationPreviewError(
            "approval_required", "non-dry-run apply requires explicit approval"
        )
    _, preview, spec, diffs, preview_bytes = _verify_published_preview(preview_dir)
    corpus_dir = _verify_current_corpus(spec)
    source, target = _resolve_apply_target(
        source_root, target_root, corpus_dir, forbidden_roots
    )

    if dry_run:
        staging = Path(tempfile.mkdtemp(prefix=".translation-dry-run-"))
        try:
            changed_files = _apply_diffs_to_tree(
                source,
                staging,
                diffs,
                fallback_encoding=fallback_encoding,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return TranslationApplyResult(
            status="dry_run",
            preview_id=preview["preview_id"],
            changed_file_count=changed_files,
            changed_unit_count=len(diffs),
            reason="validated and staged in a temporary directory; no target was published",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=str(target.parent))
    )
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True, symlinks=False)
        changed_files = _apply_diffs_to_tree(
            staging,
            staging,
            diffs,
            fallback_encoding=fallback_encoding,
        )
        apply_manifest = {
            "apply_version": 1,
            "preview_id": preview["preview_id"],
            "preview_sha256": _sha256(preview_bytes),
            "changed_file_count": changed_files,
            "changed_unit_count": len(diffs),
            "source_root": str(source),
            "dry_run": False,
        }
        with (staging / "translation-apply.json").open(
            "x", encoding="utf-8", newline="\n"
        ) as stream:
            json.dump(
                apply_manifest, stream, ensure_ascii=False, indent=2, sort_keys=True
            )
            stream.write("\n")
        try:
            os.rename(staging, target)
        except FileExistsError as exc:
            raise TranslationPreviewError(
                "output_exists", "apply target appeared during publish"
            ) from exc
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return TranslationApplyResult(
        status="applied",
        preview_id=preview["preview_id"],
        changed_file_count=changed_files,
        changed_unit_count=len(diffs),
        output_dir=str(target),
        reason="translated sidecar tree published atomically",
    )


__all__ = [
    "TRANSLATION_PREVIEW_VERSION",
    "TranslationApplyResult",
    "TranslationDiff",
    "TranslationPreviewError",
    "TranslationPreviewPublishResult",
    "TranslationPreviewReport",
    "TranslationPreviewSpec",
    "apply_translation_preview",
    "build_translation_patch_preview",
    "publish_translation_preview",
]
