"""Manifest-driven planning and transactional QLIE corpus generation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..segments import (
    MAX_JSONL_LINE_BYTES,
    MAX_JSONL_SEGMENTS,
    SEGMENT_SCHEMA_VERSION,
    SegmentContractError,
    TextSegment,
    validate_segment_collection,
)
from .exporter import _commit_staging_directory
from .script import (
    MAX_MANIFEST_BYTES,
    MAX_SCRIPT_BYTES,
    MAX_SURVEY_BYTES,
    MAX_SURVEY_FILES,
    _line_shape,
    _safe_output_path,
    parse_qlie_script_bytes,
)


CORPUS_SCHEMA_VERSION = 1
DEFAULT_RECALL_TARGET = 0.99
MAX_REVIEW_BYTES = 32 * 1024 * 1024
REVIEW_LABELS = frozenset({"translatable", "not_translatable"})


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


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _natural_key(value):
    import re

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(value).replace("\\", "/"))
    )


@dataclass(frozen=True)
class QlieCorpusFilePlan:
    output_path: str
    archive_name: str
    internal_path: str
    entry_index: int | None
    conflict_group: str | None
    status: str
    reason: str
    source_sha256: str | None = None
    source_size_bytes: int | None = None
    encoding: str | None = None
    line_count: int = 0
    nonblank_line_count: int = 0
    segment_count: int = 0
    translatable_count: int = 0
    unknown_count: int = 0
    kind_counts: tuple[tuple[str, int], ...] = ()

    def to_dict(self):
        return {
            "output_path": self.output_path,
            "archive_name": self.archive_name,
            "internal_path": self.internal_path,
            "entry_index": self.entry_index,
            "conflict_group": self.conflict_group,
            "status": self.status,
            "reason": self.reason,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "encoding": self.encoding,
            "line_count": self.line_count,
            "nonblank_line_count": self.nonblank_line_count,
            "segment_count": self.segment_count,
            "translatable_count": self.translatable_count,
            "unknown_count": self.unknown_count,
            "kind_counts": dict(self.kind_counts),
        }


@dataclass(frozen=True)
class QlieUnknownReview:
    status: str
    reason: str
    expected_count: int
    reviewed_count: int = 0
    missed_translatable_count: int = 0
    recall: float | None = None
    recall_target: float = DEFAULT_RECALL_TARGET
    review_sha256: str | None = None
    shape_counts: tuple[tuple[str, int], ...] = ()

    def to_dict(self):
        return {
            "status": self.status,
            "reason": self.reason,
            "expected_count": self.expected_count,
            "reviewed_count": self.reviewed_count,
            "missed_translatable_count": self.missed_translatable_count,
            "recall": self.recall,
            "recall_target": self.recall_target,
            "review_sha256": self.review_sha256,
            "shape_counts": dict(self.shape_counts),
        }


@dataclass(frozen=True)
class QlieCorpusPlan:
    schema_version: int
    engine: str
    source_dir: str
    output_dir: str
    source_manifest_path: str
    source_manifest_sha256: str | None
    status: str
    reason: str
    files: tuple[QlieCorpusFilePlan, ...] = ()
    unknown_review: QlieUnknownReview = field(
        default_factory=lambda: QlieUnknownReview(
            status="not_required",
            reason="corpus contains no unknown segments",
            expected_count=0,
            reviewed_count=0,
            recall=1.0,
        )
    )
    segments: tuple[TextSegment, ...] = field(default=(), repr=False)
    source_manifest_bytes: bytes = field(default=b"", repr=False)
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        kind_counts = Counter(segment.kind for segment in self.segments)
        conflict_groups = {
            item.conflict_group for item in self.files if item.conflict_group is not None
        }
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "source_dir": self.source_dir,
            "output_dir": self.output_dir,
            "source_manifest_path": self.source_manifest_path,
            "source_manifest_sha256": self.source_manifest_sha256,
            "status": self.status,
            "reason": self.reason,
            "summary": {
                "file_count": len(self.files),
                "ready_file_count": sum(item.status == "ready" for item in self.files),
                "source_bytes": sum(item.source_size_bytes or 0 for item in self.files),
                "line_count": sum(item.line_count for item in self.files),
                "nonblank_line_count": sum(
                    item.nonblank_line_count for item in self.files
                ),
                "segment_count": len(self.segments),
                "translatable_count": sum(
                    segment.translatable for segment in self.segments
                ),
                "unknown_count": kind_counts.get("unknown", 0),
                "layer_file_count": sum(
                    item.output_path.startswith("layers/") for item in self.files
                ),
                "conflict_variant_count": sum(
                    item.conflict_group is not None for item in self.files
                ),
                "conflict_group_count": len(conflict_groups),
                "kind_counts": dict(sorted(kind_counts.items())),
            },
            "unknown_review": self.unknown_review.to_dict(),
            "files": [item.to_dict() for item in self.files],
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class QlieCorpusResult:
    schema_version: int
    engine: str
    status: str
    reason: str
    output_dir: str
    segments_path: str | None
    parse_report_path: str | None
    source_manifest_path: str | None
    segment_count: int
    translatable_count: int
    unknown_count: int
    segments_sha256: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "status": self.status,
            "reason": self.reason,
            "output_dir": self.output_dir,
            "artifacts": {
                "segments_path": self.segments_path,
                "parse_report_path": self.parse_report_path,
                "source_manifest_path": self.source_manifest_path,
                "segments_sha256": self.segments_sha256,
            },
            "summary": {
                "segment_count": self.segment_count,
                "translatable_count": self.translatable_count,
                "unknown_count": self.unknown_count,
            },
            "warnings": list(self.warnings),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def _empty_plan(source_dir, output_dir, status, reason, *, warnings=()):
    source = Path(source_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    return QlieCorpusPlan(
        schema_version=CORPUS_SCHEMA_VERSION,
        engine="qlie",
        source_dir=str(source),
        output_dir=str(output),
        source_manifest_path=str(source / "manifest.json"),
        source_manifest_sha256=None,
        status=status,
        reason=reason,
        warnings=tuple(warnings),
    )


def _validate_output_location(source, output):
    if output.parent == output:
        raise ValueError("filesystem root cannot be used as a corpus output directory")
    if _is_within(output, source) or _is_within(source, output):
        raise ValueError("corpus output directory must not overlap the Phase 1 export")
    git_root = _git_root_for(output)
    if git_root is not None:
        raise ValueError(f"corpus output directory must be outside Git worktree: {git_root}")


def _verify_segment_span(data, segment):
    codecs = {
        "utf-16-le-bom": "utf-16-le",
        "utf-16-le": "utf-16-le",
        "utf-16-be-bom": "utf-16-be",
        "utf-16-be": "utf-16-be",
        "utf-8-bom": "utf-8",
        "utf-8": "utf-8",
        "cp932": "cp932",
    }
    codec = codecs.get(segment.source.encoding)
    if codec is None:
        raise SegmentContractError(
            f"unsupported source encoding in segment span: {segment.source.encoding}"
        )
    start = segment.source.byte_start
    end = segment.source.byte_end
    if data[start:end].decode(codec, errors="strict") != segment.source_text:
        raise SegmentContractError("segment byte span cannot be read back from source")


def _unknown_shape_counts(segments):
    return tuple(
        sorted(
            Counter(
                _line_shape(segment.source_text)
                for segment in segments
                if segment.kind == "unknown"
            ).items()
        )
    )


def _review_result(segments, review_path, recall_target):
    unknown = tuple(segment for segment in segments if segment.kind == "unknown")
    shapes = _unknown_shape_counts(segments)
    if not unknown:
        return QlieUnknownReview(
            status="not_required",
            reason="corpus contains no unknown segments",
            expected_count=0,
            reviewed_count=0,
            recall=1.0,
            recall_target=recall_target,
            shape_counts=shapes,
        )
    if review_path is None:
        return QlieUnknownReview(
            status="required",
            reason="all unknown segments require local stratified review before apply",
            expected_count=len(unknown),
            recall_target=recall_target,
            shape_counts=shapes,
        )
    path = Path(review_path).expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        return QlieUnknownReview(
            status="invalid",
            reason="unknown review must be a regular JSONL file",
            expected_count=len(unknown),
            recall_target=recall_target,
            shape_counts=shapes,
        )
    if path.stat().st_size > MAX_REVIEW_BYTES:
        return QlieUnknownReview(
            status="invalid",
            reason="unknown review exceeds the size limit",
            expected_count=len(unknown),
            recall_target=recall_target,
            shape_counts=shapes,
        )
    expected = {segment.segment_id: segment for segment in unknown}
    reviewed = {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return QlieUnknownReview(
            status="invalid",
            reason=f"unknown review could not be read: {exc.__class__.__name__}",
            expected_count=len(unknown),
            recall_target=recall_target,
            shape_counts=shapes,
        )
    try:
        text = raw.decode("utf-8-sig")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                raise ValueError(f"blank line at review line {line_number}")
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"review line {line_number} must be an object")
            segment_id = item.get("segment_id")
            if segment_id in reviewed:
                raise ValueError(f"duplicate segment_id at review line {line_number}")
            segment = expected.get(segment_id)
            if segment is None:
                raise ValueError(f"unexpected segment_id at review line {line_number}")
            if item.get("source_text_sha256") != segment.source_text_sha256:
                raise ValueError(f"text hash mismatch at review line {line_number}")
            if item.get("shape") != _line_shape(segment.source_text):
                raise ValueError(f"shape mismatch at review line {line_number}")
            if item.get("source_text") != segment.source_text:
                raise ValueError(f"source text mismatch at review line {line_number}")
            label = item.get("label")
            if label not in REVIEW_LABELS:
                raise ValueError(
                    f"label must be translatable or not_translatable at review line {line_number}"
                )
            reviewed[segment_id] = label
        if set(reviewed) != set(expected):
            missing = len(set(expected) - set(reviewed))
            raise ValueError(f"review is incomplete: {missing} unknown segments are missing")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return QlieUnknownReview(
            status="invalid",
            reason=f"unknown review validation failed: {exc}",
            expected_count=len(unknown),
            reviewed_count=len(reviewed),
            recall_target=recall_target,
            review_sha256=_sha256_bytes(raw),
            shape_counts=shapes,
        )
    missed = sum(label == "translatable" for label in reviewed.values())
    detected = sum(segment.translatable for segment in segments)
    recall = detected / (detected + missed) if detected + missed else 1.0
    status = "accepted" if recall >= recall_target else "below_target"
    reason = (
        "unknown review is complete and meets the corpus recall target"
        if status == "accepted"
        else "reviewed missed text lowers parser recall below the corpus target"
    )
    return QlieUnknownReview(
        status=status,
        reason=reason,
        expected_count=len(unknown),
        reviewed_count=len(reviewed),
        missed_translatable_count=missed,
        recall=recall,
        recall_target=recall_target,
        review_sha256=_sha256_bytes(raw),
        shape_counts=shapes,
    )


def build_qlie_corpus_plan(
    source_dir,
    output_dir,
    *,
    review_path=None,
    recall_target=DEFAULT_RECALL_TARGET,
    max_files=MAX_SURVEY_FILES,
    max_total_bytes=MAX_SURVEY_BYTES,
    max_file_bytes=MAX_SCRIPT_BYTES,
):
    """Parse and validate a complete Phase 1 export without writing corpus files."""

    source = Path(source_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    manifest_path = source / "manifest.json"
    try:
        if not 0 < recall_target <= 1:
            raise ValueError("recall target must be greater than zero and at most one")
        if not source.is_dir():
            raise ValueError("Phase 1 export directory does not exist")
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("manifest.json is missing or not a regular file")
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("manifest.json exceeds the corpus planning size limit")
        if output.exists():
            raise ValueError("corpus output directory already exists; overwrite is disabled")
        _validate_output_location(source, output)
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
        items = manifest.get("items") if isinstance(manifest, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("manifest items must be an array of objects")
        exported = [item for item in items if item.get("status") == "exported"]
        if not exported:
            raise ValueError("manifest contains no exported scripts")
        if len(exported) > max_files:
            raise ValueError("exported script count exceeds the corpus planning limit")
        paths = set()
        total_bytes = 0
        for item in exported:
            relative = _safe_output_path(item.get("output_path"))
            path_text = relative.as_posix()
            path_key = path_text.casefold()
            if path_key in paths:
                raise ValueError("manifest contains duplicate exported output paths")
            paths.add(path_key)
            size = item.get("decoded_size")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError("manifest contains an invalid decoded_size")
            if size > max_file_bytes:
                raise ValueError("an exported script exceeds the corpus file size limit")
            total_bytes += size
        if total_bytes > max_total_bytes:
            raise ValueError("exported scripts exceed the corpus total size limit")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return _empty_plan(source, output, "invalid_input", str(exc))

    file_plans = []
    all_segments = []
    failures = []
    for item in sorted(exported, key=lambda value: _natural_key(value["output_path"])):
        relative = _safe_output_path(item["output_path"])
        target = source.joinpath(*relative.parts)
        try:
            resolved = target.resolve(strict=True)
            if target.is_symlink() or not resolved.is_file() or not _is_within(resolved, source):
                raise ValueError("exported script is not a regular in-workspace file")
            actual_size = resolved.stat().st_size
            if actual_size > max_file_bytes:
                raise ValueError("source file exceeds the corpus file size limit")
            if actual_size != item.get("decoded_size"):
                raise ValueError("source size does not match the Phase 1 manifest")
            data = resolved.read_bytes()
            if len(data) != actual_size:
                raise ValueError("source size does not match the Phase 1 manifest")
            result = parse_qlie_script_bytes(
                data,
                archive_name=item.get("archive_name", ""),
                internal_path=item.get("internal_path", ""),
                output_path=relative.as_posix(),
                entry_index=item.get("entry_index"),
                conflict_group=item.get("conflict_group"),
                expected_sha256=item.get("decoded_sha256"),
            )
            if result.status != "supported":
                raise ValueError(f"{result.status}: {result.reason}")
            for segment in result.segments:
                _verify_segment_span(data, segment)
            kind_counts = Counter(segment.kind for segment in result.segments)
            file_plans.append(
                QlieCorpusFilePlan(
                    output_path=result.output_path,
                    archive_name=result.archive_name,
                    internal_path=result.internal_path,
                    entry_index=result.entry_index,
                    conflict_group=result.conflict_group,
                    status="ready",
                    reason="source hash, parse, and byte spans validated",
                    source_sha256=result.source_sha256,
                    source_size_bytes=result.source_size_bytes,
                    encoding=result.encoding,
                    line_count=result.line_count,
                    nonblank_line_count=result.nonblank_line_count,
                    segment_count=len(result.segments),
                    translatable_count=sum(
                        segment.translatable for segment in result.segments
                    ),
                    unknown_count=kind_counts.get("unknown", 0),
                    kind_counts=tuple(sorted(kind_counts.items())),
                )
            )
            all_segments.extend(result.segments)
        except (OSError, UnicodeDecodeError, ValueError, SegmentContractError) as exc:
            failures.append(relative.as_posix())
            file_plans.append(
                QlieCorpusFilePlan(
                    output_path=relative.as_posix(),
                    archive_name=item.get("archive_name", ""),
                    internal_path=item.get("internal_path", ""),
                    entry_index=item.get("entry_index"),
                    conflict_group=item.get("conflict_group"),
                    status="blocked",
                    reason=str(exc),
                )
            )
    try:
        segments = validate_segment_collection(all_segments)
        if len(segments) > MAX_JSONL_SEGMENTS:
            raise SegmentContractError("corpus exceeds the Segment v1 count limit")
    except SegmentContractError as exc:
        return QlieCorpusPlan(
            schema_version=CORPUS_SCHEMA_VERSION,
            engine="qlie",
            source_dir=str(source),
            output_dir=str(output),
            source_manifest_path=str(manifest_path),
            source_manifest_sha256=_sha256_bytes(manifest_bytes),
            status="blocked",
            reason=f"corpus Segment v1 validation failed: {exc}",
            files=tuple(file_plans),
            source_manifest_bytes=manifest_bytes,
        )
    review = _review_result(segments, review_path, recall_target)
    if failures:
        status = "blocked"
        reason = f"{len(failures)} source files failed corpus validation"
    elif review.status in {"required", "invalid"}:
        status = "review_required"
        reason = review.reason
    elif review.status == "below_target":
        status = "recall_below_target"
        reason = review.reason
    else:
        status = "ready"
        reason = "all source files and Segment v1 values are ready for transactional publish"
    return QlieCorpusPlan(
        schema_version=CORPUS_SCHEMA_VERSION,
        engine="qlie",
        source_dir=str(source),
        output_dir=str(output),
        source_manifest_path=str(manifest_path),
        source_manifest_sha256=_sha256_bytes(manifest_bytes),
        status=status,
        reason=reason,
        files=tuple(file_plans),
        unknown_review=review,
        segments=segments,
        source_manifest_bytes=manifest_bytes,
    )


def _review_template_item(segment):
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "segment_id": segment.segment_id,
        "source_text_sha256": segment.source_text_sha256,
        "shape": _line_shape(segment.source_text),
        "label": None,
        "source": {
            "output_path": segment.source.output_path,
            "line_start": segment.source.line_start,
            "line_end": segment.source.line_end,
        },
        "source_text": segment.source_text,
    }


def write_unknown_review_template(plan, review_path):
    """Explicitly write a local, complete, shape-stratified unknown review template."""

    path = Path(review_path).expanduser().resolve()
    if path.exists():
        raise ValueError("unknown review template already exists; overwrite is disabled")
    if path.parent == path:
        raise ValueError("filesystem root cannot be used as an unknown review file")
    git_root = _git_root_for(path)
    if git_root is not None:
        raise ValueError(f"unknown review template must be outside Git worktree: {git_root}")
    source = Path(plan.source_dir)
    if _is_within(path, source):
        raise ValueError("unknown review template must be outside the Phase 1 export")
    unknown = sorted(
        (segment for segment in plan.segments if segment.kind == "unknown"),
        key=lambda segment: (
            _line_shape(segment.source_text),
            _natural_key(segment.source.output_path),
            segment.source.byte_start,
            segment.segment_id,
        ),
    )
    if not unknown:
        raise ValueError("corpus has no unknown segments to review")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.staging-", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for segment in unknown:
                stream.write(
                    json.dumps(
                        _review_template_item(segment),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)
    return path


def _result_from_plan(plan, status, reason, *, segments_sha256=None):
    output = Path(plan.output_dir)
    published = status == "published"
    summary = plan.to_dict()["summary"]
    return QlieCorpusResult(
        schema_version=CORPUS_SCHEMA_VERSION,
        engine="qlie",
        status=status,
        reason=reason,
        output_dir=plan.output_dir,
        segments_path=str(output / "segments.jsonl") if published else None,
        parse_report_path=str(output / "parse-report.json") if published else None,
        source_manifest_path=str(output / "source-manifest.json") if published else None,
        segment_count=summary["segment_count"],
        translatable_count=summary["translatable_count"],
        unknown_count=summary["unknown_count"],
        segments_sha256=segments_sha256,
        warnings=plan.warnings,
    )


def _revalidate_sources(plan):
    manifest_path = Path(plan.source_manifest_path)
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or _sha256_file(manifest_path) != plan.source_manifest_sha256
    ):
        raise ValueError("source manifest changed after corpus planning")
    source = Path(plan.source_dir)
    by_path = {item.output_path: item for item in plan.files}
    segments_by_path = {}
    for segment in plan.segments:
        segments_by_path.setdefault(segment.source.output_path, []).append(segment)
    for output_path in sorted(by_path, key=_natural_key):
        item = by_path[output_path]
        relative = PurePosixPath(output_path)
        target = source.joinpath(*relative.parts)
        resolved = target.resolve(strict=True)
        if target.is_symlink() or not resolved.is_file() or not _is_within(resolved, source):
            raise ValueError(f"source file became unsafe after planning: {output_path}")
        if resolved.stat().st_size != item.source_size_bytes:
            raise ValueError(f"source size changed after planning: {output_path}")
        data = resolved.read_bytes()
        if _sha256_bytes(data) != item.source_sha256:
            raise ValueError(f"source hash changed after planning: {output_path}")
        for segment in segments_by_path.get(output_path, ()):
            _verify_segment_span(data, segment)
    validate_segment_collection(plan.segments)


def _write_segments(path, segments):
    digest = hashlib.sha256()
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for segment in segments:
            line = json.dumps(
                segment.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n"
            encoded = line.encode("utf-8")
            if len(encoded) > MAX_JSONL_LINE_BYTES:
                raise ValueError("a Segment v1 JSONL line exceeds the protocol size limit")
            digest.update(encoded)
            stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    written_hash = _sha256_file(path)
    if written_hash != digest.hexdigest():
        raise OSError("segments.jsonl hash changed during staging verification")
    return written_hash


def apply_qlie_corpus_plan(plan):
    """Revalidate and atomically publish a complete corpus to a new directory."""

    if plan.status != "ready":
        return _result_from_plan(
            plan, "blocked", f"corpus plan is not ready: {plan.reason}"
        )
    output = Path(plan.output_dir)
    if output.exists():
        return _result_from_plan(
            plan, "output_exists", "corpus output already exists; overwrite is disabled"
        )
    try:
        _validate_output_location(Path(plan.source_dir), output)
        _revalidate_sources(plan)
    except (OSError, UnicodeDecodeError, ValueError, SegmentContractError) as exc:
        return _result_from_plan(plan, "source_changed", str(exc))

    staging = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            return _result_from_plan(
                plan,
                "output_exists",
                "corpus output appeared during preflight; overwrite is disabled",
            )
        staging = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=str(output.parent))
        )
        segments_hash = _write_segments(staging / "segments.jsonl", plan.segments)
        from ..characters import publish_catalogue, qlie_characters

        publish_catalogue(staging, qlie_characters(plan.segments, segments_hash))
        report = plan.to_dict()
        report["status"] = "published"
        report["reason"] = "corpus published transactionally"
        report["artifacts"] = {
            "segments": {
                "path": "segments.jsonl",
                "sha256": segments_hash,
            },
            "source_manifest": {
                "path": "source-manifest.json",
                "sha256": plan.source_manifest_sha256,
            },
        }
        with (staging / "parse-report.json").open(
            "x", encoding="utf-8", newline="\n"
        ) as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        with (staging / "source-manifest.json").open("xb") as stream:
            stream.write(plan.source_manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if _sha256_file(staging / "source-manifest.json") != plan.source_manifest_sha256:
            raise OSError("source manifest hash changed during staging verification")
        _commit_staging_directory(staging, output)
        staging = None
    except (OSError, TypeError, ValueError) as exc:
        return _result_from_plan(
            plan, "write_failed", f"transactional corpus publish failed: {exc}"
        )
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return _result_from_plan(
        plan,
        "published",
        "corpus published transactionally to a new directory",
        segments_sha256=segments_hash,
    )


def render_corpus_plan_text(plan):
    summary = plan.to_dict()["summary"]
    review = plan.unknown_review
    return "\n".join(
        [
            f"QLIE corpus plan: {plan.status}",
            f"source_dir: {plan.source_dir}",
            f"output_dir: {plan.output_dir}",
            (
                f"files: {summary['ready_file_count']}/{summary['file_count']}; "
                f"segments: {summary['segment_count']}; "
                f"translatable: {summary['translatable_count']}; "
                f"unknown: {summary['unknown_count']}"
            ),
            (
                f"layers: {summary['layer_file_count']} files; "
                f"conflict variants: {summary['conflict_variant_count']} "
                f"in {summary['conflict_group_count']} groups"
            ),
            (
                f"unknown review: {review.status}; "
                f"reviewed: {review.reviewed_count}/{review.expected_count}; "
                f"recall: {review.recall if review.recall is not None else 'pending'} "
                f"(target {review.recall_target})"
            ),
            f"reason: {plan.reason}",
        ]
    )


def render_corpus_result_text(result):
    return "\n".join(
        [
            f"QLIE corpus publish: {result.status}",
            f"output_dir: {result.output_dir}",
            (
                f"segments: {result.segment_count}; "
                f"translatable: {result.translatable_count}; "
                f"unknown: {result.unknown_count}"
            ),
            f"segments.jsonl: {result.segments_path or 'not written'}",
            f"parse-report.json: {result.parse_report_path or 'not written'}",
            f"source-manifest.json: {result.source_manifest_path or 'not written'}",
            f"reason: {result.reason}",
        ]
    )
