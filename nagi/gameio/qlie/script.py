"""Read-only, content-free survey of scripts exported by Phase 1C."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..models import (
    QlieScriptSurveyFile,
    QlieScriptSurveyReport,
    QlieScriptSyntaxShape,
)
from ..segments import (
    SEGMENT_SCHEMA_VERSION,
    SegmentContractError,
    SegmentInlineToken,
    SegmentSource,
    build_text_segment,
    link_segment_sequence,
    segments_to_jsonl,
)
from .detector import REPORT_SCHEMA_VERSION


MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_SURVEY_FILES = 5_000
MAX_SCRIPT_BYTES = 16 * 1024 * 1024
MAX_SURVEY_BYTES = 256 * 1024 * 1024
DEFAULT_TOP_SHAPES = 30
ALLOWED_SCRIPT_EXTENSIONS = {".s", ".txt"}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ASSIGNMENT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*(?:\[[A-Za-z0-9_.]+\][A-Za-z0-9_.]*)*\s*="
)
VOICE_ACT_RE = re.compile(r"^act\[[0-9]+\]\s*=", re.IGNORECASE)
SECTION_HEADER_RE = re.compile(r"^\[[A-Za-z_][A-Za-z0-9_.-]*\]$")
PLACEHOLDER_RE = re.compile(r"\{[^{}\r\n]+\}|%[A-Za-z_][A-Za-z0-9_.]*%")
BRACKET_TAG_RE = re.compile(r"\[[^\[\]\r\n]+\]")
ESCAPE_TAG_RE = re.compile(r"\\[nrt\\]")
TEXT_CARET_COMMANDS = ("^savedate", "^saveroute", "^savescene")
MAX_PARSE_WARNINGS = 100


@dataclass(frozen=True)
class QlieScriptLine:
    """One decoded physical line with exact offsets into the original bytes."""

    number: int
    text: str
    byte_start: int
    byte_end: int
    newline_byte_start: int
    newline_byte_end: int
    newline: str

    def to_dict(self, *, include_text=False):
        payload = {
            "number": self.number,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "newline_byte_start": self.newline_byte_start,
            "newline_byte_end": self.newline_byte_end,
            "newline": self.newline,
            "character_count": len(self.text),
        }
        if include_text:
            payload["text"] = self.text
        return payload


@dataclass(frozen=True)
class QlieScriptScan:
    """Lossless line-scan result for one decoded QLIE script."""

    encoding: str
    codec: str | None
    bom_size: int
    size_bytes: int
    lines: tuple[QlieScriptLine, ...]

    def to_dict(self, *, include_text=False):
        return {
            "encoding": self.encoding,
            "bom_size": self.bom_size,
            "size_bytes": self.size_bytes,
            "line_count": len(self.lines),
            "lines": [line.to_dict(include_text=include_text) for line in self.lines],
        }


@dataclass(frozen=True)
class QlieScriptParseResult:
    """Structured result for conservative parsing of one exported QLIE script."""

    schema_version: int
    engine: str
    status: str
    reason: str
    archive_name: str
    internal_path: str
    output_path: str
    entry_index: int | None
    conflict_group: str | None
    source_sha256: str | None
    source_size_bytes: int | None
    encoding: str | None
    line_count: int
    nonblank_line_count: int
    segments: tuple
    warnings: tuple[str, ...] = ()

    def to_dict(self, *, include_segments=False):
        kind_counts = Counter(segment.kind for segment in self.segments)
        payload = {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "status": self.status,
            "reason": self.reason,
            "source": {
                "archive_name": self.archive_name,
                "internal_path": self.internal_path,
                "output_path": self.output_path,
                "entry_index": self.entry_index,
                "conflict_group": self.conflict_group,
                "source_sha256": self.source_sha256,
                "source_size_bytes": self.source_size_bytes,
                "encoding": self.encoding,
            },
            "summary": {
                "line_count": self.line_count,
                "nonblank_line_count": self.nonblank_line_count,
                "segment_count": len(self.segments),
                "translatable_count": sum(
                    segment.translatable for segment in self.segments
                ),
                "unknown_count": kind_counts.get("unknown", 0),
                "kind_counts": dict(sorted(kind_counts.items())),
            },
            "warnings": list(self.warnings),
        }
        if include_segments:
            payload["segments"] = [segment.to_dict() for segment in self.segments]
        return payload

    def to_json(self, *, include_segments=False):
        return json.dumps(
            self.to_dict(include_segments=include_segments),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    def to_jsonl(self):
        return segments_to_jsonl(self.segments)


def _empty_report(export_dir, status, reason, *, manifest_item_count=0, warnings=()):
    export_path = Path(export_dir).expanduser().resolve()
    return QlieScriptSurveyReport(
        schema_version=REPORT_SCHEMA_VERSION,
        engine="qlie",
        export_dir=str(export_path),
        manifest_path=str(export_path / "manifest.json"),
        status=status,
        reason=reason,
        manifest_item_count=manifest_item_count,
        warnings=tuple(warnings),
    )


def _safe_output_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("manifest output_path must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("manifest output_path is absolute or contains traversal")
    if path.suffix.casefold() not in ALLOWED_SCRIPT_EXTENSIONS:
        raise ValueError("manifest output_path is not an allowlisted script")
    return path


def _is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _detect_script_encoding(data):
    if not data:
        return "empty", None, 0
    bom_codecs = (
        (b"\xff\xfe", "utf-16-le", "utf-16-le-bom"),
        (b"\xfe\xff", "utf-16-be", "utf-16-be-bom"),
        (b"\xef\xbb\xbf", "utf-8", "utf-8-bom"),
    )
    for bom, codec, label in bom_codecs:
        if data.startswith(bom):
            data[len(bom) :].decode(codec, errors="strict")
            return label, codec, len(bom)

    even = data[0::2]
    odd = data[1::2]
    even_nul_ratio = even.count(0) / max(1, len(even))
    odd_nul_ratio = odd.count(0) / max(1, len(odd))
    if odd_nul_ratio >= 0.2 and even_nul_ratio <= 0.05:
        data.decode("utf-16-le", errors="strict")
        return "utf-16-le", "utf-16-le", 0
    if even_nul_ratio >= 0.2 and odd_nul_ratio <= 0.05:
        data.decode("utf-16-be", errors="strict")
        return "utf-16-be", "utf-16-be", 0

    try:
        data.decode("utf-8", errors="strict")
        return "utf-8", "utf-8", 0
    except UnicodeDecodeError:
        data.decode("cp932", errors="strict")
        return "cp932", "cp932", 0


def _decode_script(data):
    encoding, codec, bom_size = _detect_script_encoding(data)
    if codec is None:
        return "", encoding, False
    return data[bom_size:].decode(codec, errors="strict"), encoding, bool(bom_size)


def scan_qlie_script_lines(data):
    """Decode a script and retain exact byte spans for every physical line."""

    if not isinstance(data, bytes):
        raise TypeError("QLIE script data must be bytes")
    encoding, codec, bom_size = _detect_script_encoding(data)
    if codec is None:
        return QlieScriptScan(
            encoding=encoding,
            codec=None,
            bom_size=0,
            size_bytes=0,
            lines=(),
        )
    cr = "\r".encode(codec)
    lf = "\n".encode(codec)
    crlf = cr + lf
    unit = 2 if codec.startswith("utf-16") else 1
    lines = []
    line_start = bom_size
    cursor = bom_size
    number = 1
    while cursor < len(data):
        newline = None
        newline_bytes = 0
        if data.startswith(crlf, cursor):
            newline = "crlf"
            newline_bytes = len(crlf)
        elif data.startswith(cr, cursor):
            newline = "cr"
            newline_bytes = len(cr)
        elif data.startswith(lf, cursor):
            newline = "lf"
            newline_bytes = len(lf)
        if newline is None:
            cursor += unit
            continue
        text = data[line_start:cursor].decode(codec, errors="strict")
        lines.append(
            QlieScriptLine(
                number=number,
                text=text,
                byte_start=line_start,
                byte_end=cursor,
                newline_byte_start=cursor,
                newline_byte_end=cursor + newline_bytes,
                newline=newline,
            )
        )
        number += 1
        cursor += newline_bytes
        line_start = cursor
    if line_start < len(data):
        text = data[line_start:].decode(codec, errors="strict")
        lines.append(
            QlieScriptLine(
                number=number,
                text=text,
                byte_start=line_start,
                byte_end=len(data),
                newline_byte_start=len(data),
                newline_byte_end=len(data),
                newline="none",
            )
        )
    return QlieScriptScan(
        encoding=encoding,
        codec=codec,
        bom_size=bom_size,
        size_bytes=len(data),
        lines=tuple(lines),
    )


def _newline_style(text):
    crlf_count = text.count("\r\n")
    remainder = text.replace("\r\n", "")
    lf_count = remainder.count("\n")
    cr_count = remainder.count("\r")
    present = [
        name
        for name, count in (("crlf", crlf_count), ("lf", lf_count), ("cr", cr_count))
        if count
    ]
    if not present:
        return "none"
    if len(present) == 1:
        return present[0]
    return "mixed"


def _line_family(line):
    stripped = line.strip()
    if not stripped:
        return "blank"
    if stripped.startswith(("//", ";")):
        return "comment_like"
    if stripped.startswith("@@"):
        return "at_label"
    if stripped.startswith("@"):
        return "at_command"
    if stripped.startswith("\\"):
        return "backslash_command"
    if stripped.startswith("^"):
        return "caret_expression"
    if stripped.startswith(("[", "{", "(")):
        return "bracket_form"
    if ASSIGNMENT_RE.match(stripped):
        return "assignment"
    if any(ord(char) > 127 and char.isprintable() for char in stripped):
        return "non_ascii_text_candidate"
    if any(char.isalnum() for char in stripped):
        return "ascii_text_or_data"
    return "symbol_or_other"


def _line_shape(line, limit=96):
    stripped = line.strip()
    if not stripped:
        return "<blank>"
    tokens = []
    previous_category = None
    for char in stripped:
        if char.isspace():
            category = "_"
        elif "A" <= char <= "Z" or "a" <= char <= "z":
            category = "A"
        elif "0" <= char <= "9":
            category = "N"
        elif ord(char) > 127 and char.isprintable():
            category = "T"
        elif char.isprintable():
            category = char
        else:
            category = "U"
        collapsible = category in {"_", "A", "N", "T", "U"}
        if not collapsible or category != previous_category:
            tokens.append(category)
        previous_category = category
        if len(tokens) >= limit:
            tokens.append("...")
            break
    return "".join(tokens)


def _survey_one(root, item, shape_counts, *, max_file_bytes):
    output_path = item["output_path"]
    archive_name = item.get("archive_name", "")
    internal_path = item.get("internal_path", "")
    conflict_group = item.get("conflict_group")
    expected_size = item["decoded_size"]
    expected_sha256 = item["decoded_sha256"].lower()
    relative = _safe_output_path(output_path)
    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        return QlieScriptSurveyFile(
            output_path=output_path,
            archive_name=archive_name,
            internal_path=internal_path,
            conflict_group=conflict_group,
            status="missing",
            reason=f"exported script is unavailable: {exc.__class__.__name__}",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
    if not _is_within(resolved, root) or candidate.is_symlink() or not resolved.is_file():
        return QlieScriptSurveyFile(
            output_path=output_path,
            archive_name=archive_name,
            internal_path=internal_path,
            conflict_group=conflict_group,
            status="unsafe_path",
            reason="exported script is not a regular in-workspace file",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
    size_bytes = resolved.stat().st_size
    if size_bytes > max_file_bytes:
        return QlieScriptSurveyFile(
            output_path=output_path,
            archive_name=archive_name,
            internal_path=internal_path,
            conflict_group=conflict_group,
            status="limit_exceeded",
            reason=f"script exceeds per-file survey limit: {size_bytes} > {max_file_bytes}",
            size_bytes=size_bytes,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
    if size_bytes != expected_size:
        return QlieScriptSurveyFile(
            output_path=output_path,
            archive_name=archive_name,
            internal_path=internal_path,
            conflict_group=conflict_group,
            status="size_mismatch",
            reason="script size does not match the Phase 1 manifest",
            size_bytes=size_bytes,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        return QlieScriptSurveyFile(
            output_path=output_path,
            archive_name=archive_name,
            internal_path=internal_path,
            conflict_group=conflict_group,
            status="read_failed",
            reason=f"script read failed: {exc.__class__.__name__}",
            size_bytes=size_bytes,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        return QlieScriptSurveyFile(
            output_path=output_path,
            archive_name=archive_name,
            internal_path=internal_path,
            conflict_group=conflict_group,
            status="hash_mismatch",
            reason="script SHA-256 does not match the Phase 1 manifest",
            size_bytes=len(data),
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha256,
        )
    try:
        text, encoding, has_bom = _decode_script(data)
    except UnicodeDecodeError:
        return QlieScriptSurveyFile(
            output_path=output_path,
            archive_name=archive_name,
            internal_path=internal_path,
            conflict_group=conflict_group,
            status="unsupported_encoding",
            reason="script is not valid UTF-8, UTF-16, or CP932 text",
            size_bytes=len(data),
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha256,
        )
    lines = text.splitlines()
    families = Counter(_line_family(line) for line in lines)
    for line in lines:
        shape_counts[_line_shape(line)] += 1
    return QlieScriptSurveyFile(
        output_path=output_path,
        archive_name=archive_name,
        internal_path=internal_path,
        conflict_group=conflict_group,
        status="surveyed",
        reason="hash verified and structural survey completed",
        size_bytes=len(data),
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        actual_sha256=actual_sha256,
        encoding=encoding,
        has_bom=has_bom,
        newline_style=_newline_style(text),
        line_count=len(lines),
        blank_line_count=families.get("blank", 0),
        max_line_length=max((len(line) for line in lines), default=0),
        nul_codepoint_count=text.count("\x00"),
        line_family_counts=tuple(sorted(families.items())),
    )


def survey_script_export(
    export_dir,
    *,
    top_shapes=DEFAULT_TOP_SHAPES,
    max_files=MAX_SURVEY_FILES,
    max_file_bytes=MAX_SCRIPT_BYTES,
    max_total_bytes=MAX_SURVEY_BYTES,
):
    """Validate a Phase 1 export and report structural facts without script text."""

    export_path = Path(export_dir).expanduser().resolve()
    manifest_path = export_path / "manifest.json"
    if top_shapes < 0:
        return _empty_report(export_path, "invalid_input", "top_shapes must be zero or greater")
    if not export_path.is_dir():
        return _empty_report(export_path, "invalid_input", "export directory does not exist")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return _empty_report(export_path, "invalid_input", "manifest.json is missing or not a regular file")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        return _empty_report(export_path, "limit_exceeded", "manifest exceeds the survey size limit")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _empty_report(export_path, "invalid_input", "manifest.json is not valid UTF-8 JSON")
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, list):
        return _empty_report(export_path, "invalid_input", "manifest items must be a JSON array")
    if any(not isinstance(item, dict) for item in items):
        return _empty_report(export_path, "invalid_input", "manifest items must contain only objects")
    exported = [item for item in items if isinstance(item, dict) and item.get("status") == "exported"]
    if len(exported) > max_files:
        return _empty_report(
            export_path,
            "limit_exceeded",
            f"exported script count exceeds survey limit: {len(exported)} > {max_files}",
            manifest_item_count=len(items),
        )
    seen = set()
    expected_total = 0
    try:
        for item in exported:
            relative = _safe_output_path(item.get("output_path"))
            if not isinstance(item.get("archive_name"), str):
                raise ValueError("manifest contains an invalid archive_name")
            if not isinstance(item.get("internal_path"), str):
                raise ValueError("manifest contains an invalid internal_path")
            if item.get("conflict_group") is not None and not isinstance(
                item.get("conflict_group"), str
            ):
                raise ValueError("manifest contains an invalid conflict_group")
            key = relative.as_posix().casefold()
            if key in seen:
                raise ValueError("manifest contains duplicate output paths")
            seen.add(key)
            digest = item.get("decoded_sha256")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise ValueError("manifest contains an invalid decoded_sha256")
            decoded_size = item.get("decoded_size")
            if not isinstance(decoded_size, int) or decoded_size < 0:
                raise ValueError("manifest contains an invalid decoded_size")
            expected_total += decoded_size
    except ValueError as exc:
        return _empty_report(
            export_path,
            "invalid_input",
            str(exc),
            manifest_item_count=len(items),
        )
    if expected_total > max_total_bytes:
        return _empty_report(
            export_path,
            "limit_exceeded",
            f"manifest decoded size exceeds survey limit: {expected_total} > {max_total_bytes}",
            manifest_item_count=len(items),
        )

    shape_counts = Counter()
    files = tuple(
        _survey_one(export_path, item, shape_counts, max_file_bytes=max_file_bytes)
        for item in exported
    )
    failures = [item for item in files if item.status != "surveyed"]
    status = "partial" if failures else "supported"
    reason = (
        f"{len(failures)} exported scripts failed validation or survey"
        if failures
        else "all exported scripts passed hash validation and structural survey"
    )
    shapes = tuple(
        QlieScriptSyntaxShape(shape=shape, count=count)
        for shape, count in sorted(
            shape_counts.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )[:top_shapes]
    )
    return QlieScriptSurveyReport(
        schema_version=REPORT_SCHEMA_VERSION,
        engine="qlie",
        export_dir=str(export_path),
        manifest_path=str(manifest_path),
        status=status,
        reason=reason,
        manifest_item_count=len(items),
        files=files,
        syntax_shapes=shapes,
    )


def render_script_survey_text(report):
    summary = report.to_dict()["summary"]
    lines = [
        f"QLIE script survey: {report.status}",
        f"export_dir: {report.export_dir}",
        f"manifest: {report.manifest_path}",
        (
            f"files: {summary['surveyed_count']}/{summary['file_count']} surveyed, "
            f"{summary['total_bytes']} bytes, {summary['total_lines']} lines"
        ),
        (
            f"sources: {len(summary['archive_counts'])} archives, "
            f"{summary['conflict_variant_count']} conflict variants"
        ),
        "encodings: "
        + ", ".join(
            f"{name}={count}" for name, count in summary["encoding_counts"].items()
        ),
        "newlines: "
        + ", ".join(
            f"{name}={count}" for name, count in summary["newline_style_counts"].items()
        ),
        "line families: "
        + ", ".join(
            f"{name}={count}" for name, count in summary["line_family_counts"].items()
        ),
    ]
    if report.syntax_shapes:
        lines.append("top redacted syntax shapes:")
        lines.extend(f"  {shape.count:>7}  {shape.shape}" for shape in report.syntax_shapes)
    lines.append(f"reason: {report.reason}")
    lines.extend(f"warning: {warning}" for warning in report.warnings)
    return "\n".join(lines)


def _parse_error_result(
    status,
    reason,
    *,
    archive_name="",
    internal_path="",
    output_path="",
    entry_index=None,
    conflict_group=None,
    source_sha256=None,
    source_size_bytes=None,
    encoding=None,
    warnings=(),
):
    return QlieScriptParseResult(
        schema_version=SEGMENT_SCHEMA_VERSION,
        engine="qlie",
        status=status,
        reason=reason,
        archive_name=archive_name,
        internal_path=internal_path,
        output_path=output_path,
        entry_index=entry_index,
        conflict_group=conflict_group,
        source_sha256=source_sha256,
        source_size_bytes=source_size_bytes,
        encoding=encoding,
        line_count=0,
        nonblank_line_count=0,
        segments=(),
        warnings=tuple(warnings),
    )


def _validate_parse_identity(
    *,
    archive_name,
    internal_path,
    output_path,
    entry_index,
    conflict_group,
    expected_sha256,
):
    _safe_output_path(output_path)
    for field, value, allow_backslash in (
        ("archive_name", archive_name, False),
        ("internal_path", internal_path, True),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty relative path")
        if not allow_backslash and "\\" in value:
            raise ValueError(f"{field} must use POSIX separators")
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute()
            or ":" in path.parts[0]
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(f"{field} must be a safe relative path")
    if isinstance(entry_index, bool) or not isinstance(entry_index, int) or entry_index < 0:
        raise ValueError("entry_index must be zero or greater")
    if conflict_group is not None:
        if not isinstance(conflict_group, str):
            raise ValueError("conflict_group must be a string or null")
        path = PurePosixPath(conflict_group)
        if (
            not conflict_group
            or "\\" in conflict_group
            or path.is_absolute()
            or ":" in path.parts[0]
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("conflict_group must be a safe relative POSIX path")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256)
    ):
        raise ValueError("expected_sha256 must be a SHA-256 hex digest")


def _non_ascii_span(text, start=0, end=None):
    limit = len(text) if end is None else end
    indexes = [index for index in range(start, limit) if ord(text[index]) > 0x80]
    if not indexes:
        return None
    return indexes[0], indexes[-1] + 1


def _split_argument_spans(text, start):
    spans = []
    field_start = start
    quote = None
    escaped = False
    depth = 0
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = set(pairs.values())
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote is not None:
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char in pairs:
            depth += 1
            continue
        if char in closers and depth:
            depth -= 1
            continue
        if char == "," and depth == 0:
            spans.append((field_start, index))
            field_start = index + 1
    spans.append((field_start, len(text)))
    return spans


def _trim_span(text, start, end, *, strip_quotes=False):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if (
        strip_quotes
        and end - start >= 2
        and text[start] in {'"', "'"}
        and text[end - 1] == text[start]
    ):
        start += 1
        end -= 1
    return start, end


def _inline_tokens(source_text):
    placeholders = tuple(
        SegmentInlineToken(
            kind="placeholder",
            raw=match.group(0),
            char_start=match.start(),
            char_end=match.end(),
        )
        for match in PLACEHOLDER_RE.finditer(source_text)
    )
    tags = [
        SegmentInlineToken(
            kind="bracket_tag",
            raw=match.group(0),
            char_start=match.start(),
            char_end=match.end(),
        )
        for match in BRACKET_TAG_RE.finditer(source_text)
    ]
    tags.extend(
        SegmentInlineToken(
            kind="escape",
            raw=match.group(0),
            char_start=match.start(),
            char_end=match.end(),
        )
        for match in ESCAPE_TAG_RE.finditer(source_text)
    )
    return placeholders, tuple(
        sorted(tags, key=lambda token: (token.char_start, token.char_end, token.kind))
    )


def _span_to_source(
    *,
    data,
    scan,
    line,
    char_start,
    char_end,
    archive_name,
    internal_path,
    output_path,
    entry_index,
    conflict_group,
    source_sha256,
):
    if scan.codec is None:
        raise SegmentContractError("empty scripts cannot contain source spans")
    prefix_size = len(line.text[:char_start].encode(scan.codec))
    text_size = len(line.text[char_start:char_end].encode(scan.codec))
    byte_start = line.byte_start + prefix_size
    byte_end = byte_start + text_size
    if data[byte_start:byte_end].decode(scan.codec, errors="strict") != line.text[
        char_start:char_end
    ]:
        raise SegmentContractError("character span could not be mapped to source bytes")
    return SegmentSource(
        engine="qlie",
        archive_name=archive_name,
        internal_path=internal_path,
        output_path=output_path,
        entry_index=entry_index,
        conflict_group=conflict_group,
        source_sha256=source_sha256,
        source_size_bytes=len(data),
        encoding=scan.encoding,
        byte_start=byte_start,
        byte_end=byte_end,
        line_start=line.number,
        line_end=line.number,
    )


def parse_qlie_script_bytes(
    data,
    *,
    archive_name,
    internal_path,
    output_path,
    entry_index,
    conflict_group=None,
    expected_sha256=None,
):
    """Conservatively convert one decoded script payload into Segment v1 values."""

    if not isinstance(data, bytes):
        return _parse_error_result(
            "invalid_input",
            "QLIE script payload must be bytes",
            archive_name=archive_name,
            internal_path=internal_path,
            output_path=output_path,
            entry_index=entry_index,
            conflict_group=conflict_group,
        )
    try:
        _validate_parse_identity(
            archive_name=archive_name,
            internal_path=internal_path,
            output_path=output_path,
            entry_index=entry_index,
            conflict_group=conflict_group,
            expected_sha256=expected_sha256,
        )
    except ValueError as exc:
        return _parse_error_result(
            "invalid_input",
            str(exc),
            archive_name=archive_name,
            internal_path=internal_path,
            output_path=output_path,
            entry_index=entry_index,
            conflict_group=conflict_group,
            source_size_bytes=len(data),
        )
    source_sha256 = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and source_sha256 != expected_sha256.casefold():
        return _parse_error_result(
            "hash_mismatch",
            "script SHA-256 does not match the Phase 1 manifest",
            archive_name=archive_name,
            internal_path=internal_path,
            output_path=output_path,
            entry_index=entry_index,
            conflict_group=conflict_group,
            source_sha256=source_sha256,
            source_size_bytes=len(data),
        )
    try:
        scan = scan_qlie_script_lines(data)
    except (TypeError, UnicodeDecodeError, ValueError) as exc:
        return _parse_error_result(
            "unsupported_encoding" if isinstance(exc, UnicodeDecodeError) else "invalid_input",
            (
                "script is not valid UTF-8, UTF-16, or CP932 text"
                if isinstance(exc, UnicodeDecodeError)
                else str(exc)
            ),
            archive_name=archive_name,
            internal_path=internal_path,
            output_path=output_path,
            entry_index=entry_index,
            conflict_group=conflict_group,
            source_sha256=source_sha256,
            source_size_bytes=len(data),
        )

    segments = []
    result_warnings = []
    unknown_count = 0
    current_scene = None
    pending_speaker = None

    def add_segment(
        kind,
        line,
        start,
        end,
        *,
        translatable,
        speaker=None,
        scene=None,
        warnings=(),
    ):
        source_text = line.text[start:end]
        source = _span_to_source(
            data=data,
            scan=scan,
            line=line,
            char_start=start,
            char_end=end,
            archive_name=archive_name,
            internal_path=internal_path,
            output_path=output_path,
            entry_index=entry_index,
            conflict_group=conflict_group,
            source_sha256=source_sha256,
        )
        placeholders, tags = _inline_tokens(source_text)
        segments.append(
            build_text_segment(
                kind=kind,
                source=source,
                source_text=source_text,
                translatable=translatable,
                speaker=speaker,
                scene=scene,
                placeholders=placeholders,
                tags=tags,
                warnings=warnings,
            )
        )

    try:
        for line in scan.lines:
            stripped = line.text.strip()
            if not stripped:
                continue
            leading = len(line.text) - len(line.text.lstrip())
            lowered = stripped.casefold()
            if stripped.startswith("@@"):
                current_scene = stripped[2:] or stripped
                pending_speaker = None
                add_segment(
                    "label",
                    line,
                    0,
                    len(line.text),
                    translatable=False,
                    scene=current_scene,
                )
                continue
            if stripped.startswith(("//", ";", "％")):
                add_segment(
                    "comment",
                    line,
                    0,
                    len(line.text),
                    translatable=False,
                    scene=current_scene,
                )
                continue
            if lowered.startswith("^select"):
                command_start = leading + len("^select")
                if command_start < len(line.text) and line.text[command_start] == ",":
                    command_start += 1
                choice_count = 0
                for start, end in _split_argument_spans(line.text, command_start):
                    start, end = _trim_span(
                        line.text,
                        start,
                        end,
                        strip_quotes=True,
                    )
                    if start < end:
                        add_segment(
                            "choice",
                            line,
                            start,
                            end,
                            translatable=True,
                            scene=current_scene,
                        )
                        choice_count += 1
                if choice_count:
                    pending_speaker = None
                    continue
            if any(lowered.startswith(command) for command in TEXT_CARET_COMMANDS):
                span = _non_ascii_span(line.text)
                if span is not None:
                    add_segment(
                        "metadata",
                        line,
                        span[0],
                        span[1],
                        translatable=True,
                        scene=current_scene,
                    )
                    continue
            if lowered.startswith("[pc,"):
                closing = line.text.rfind("]")
                content_start = leading + len("[pc,")
                if closing >= content_start:
                    content_start, content_end = _trim_span(
                        line.text, content_start, closing
                    )
                    if content_start < content_end:
                        add_segment(
                            "narration",
                            line,
                            content_start,
                            content_end,
                            translatable=True,
                            scene=current_scene,
                        )
                    suffix_start, suffix_end = _trim_span(
                        line.text, closing + 1, len(line.text)
                    )
                    if suffix_start < suffix_end:
                        add_segment(
                            "narration",
                            line,
                            suffix_start,
                            suffix_end,
                            translatable=True,
                            scene=current_scene,
                        )
                    pending_speaker = None
                    continue
            if lowered.startswith("[rb,") and "]" in stripped:
                end = len(line.text.rstrip())
                add_segment(
                    "narration",
                    line,
                    leading,
                    end,
                    translatable=True,
                    scene=current_scene,
                )
                pending_speaker = None
                continue
            voice_match = VOICE_ACT_RE.match(stripped)
            if voice_match is not None:
                start, end = _trim_span(
                    line.text,
                    leading + voice_match.end(),
                    len(line.text),
                )
                if start < end and _non_ascii_span(line.text, start, end) is not None:
                    add_segment(
                        "metadata",
                        line,
                        start,
                        end,
                        translatable=True,
                        scene=current_scene,
                    )
                    pending_speaker = None
                    continue
            non_ascii = _non_ascii_span(line.text)
            prefix = line.text[: non_ascii[0]] if non_ascii is not None else line.text
            if non_ascii is not None and not prefix.strip():
                start, end = non_ascii
                source_text = line.text[start:end]
                if stripped.startswith("〖"):
                    speaker_name = source_text.strip().strip("〖〗").strip()
                    add_segment(
                        "speaker_name",
                        line,
                        start,
                        end,
                        translatable=True,
                        speaker=speaker_name or None,
                        scene=current_scene,
                    )
                    pending_speaker = speaker_name or None
                else:
                    kind = (
                        "dialogue"
                        if pending_speaker is not None
                        or source_text.lstrip().startswith(("「", "『"))
                        else "narration"
                    )
                    add_segment(
                        kind,
                        line,
                        start,
                        end,
                        translatable=True,
                        speaker=pending_speaker if kind == "dialogue" else None,
                        scene=current_scene,
                    )
                    pending_speaker = None
                continue
            if (
                stripped.startswith(("\\", "^", "@", "$"))
                or ASSIGNMENT_RE.match(stripped)
                or SECTION_HEADER_RE.fullmatch(stripped)
            ):
                add_segment(
                    "control",
                    line,
                    0,
                    len(line.text),
                    translatable=False,
                    scene=current_scene,
                )
                continue
            unknown_count += 1
            warning = f"line {line.number}: unrecognized QLIE line preserved as unknown"
            if len(result_warnings) < MAX_PARSE_WARNINGS:
                result_warnings.append(warning)
            add_segment(
                "unknown",
                line,
                0,
                len(line.text),
                translatable=False,
                scene=current_scene,
                warnings=("unrecognized QLIE line structure",),
            )
    except (SegmentContractError, UnicodeEncodeError, UnicodeDecodeError) as exc:
        return _parse_error_result(
            "parse_failed",
            f"QLIE segment construction failed: {exc}",
            archive_name=archive_name,
            internal_path=internal_path,
            output_path=output_path,
            entry_index=entry_index,
            conflict_group=conflict_group,
            source_sha256=source_sha256,
            source_size_bytes=len(data),
            encoding=scan.encoding,
        )

    if unknown_count > len(result_warnings):
        result_warnings.append(
            f"{unknown_count - len(result_warnings)} additional unknown lines omitted from warnings"
        )
    linked = link_segment_sequence(segments)
    reason = (
        f"script parsed conservatively with {unknown_count} unknown lines"
        if unknown_count
        else "script parsed into stable Segment v1 values"
    )
    return QlieScriptParseResult(
        schema_version=SEGMENT_SCHEMA_VERSION,
        engine="qlie",
        status="supported",
        reason=reason,
        archive_name=archive_name,
        internal_path=internal_path,
        output_path=output_path,
        entry_index=entry_index,
        conflict_group=conflict_group,
        source_sha256=source_sha256,
        source_size_bytes=len(data),
        encoding=scan.encoding,
        line_count=len(scan.lines),
        nonblank_line_count=sum(bool(line.text.strip()) for line in scan.lines),
        segments=linked,
        warnings=tuple(result_warnings),
    )


def parse_exported_script(export_dir, output_path):
    """Read and parse exactly one manifest-addressed Phase 1 script."""

    root = Path(export_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    if not root.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        return _parse_error_result(
            "invalid_input",
            "Phase 1 export directory or manifest.json is unavailable",
            output_path=str(output_path),
        )
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        return _parse_error_result(
            "limit_exceeded",
            "manifest exceeds the parser size limit",
            output_path=str(output_path),
        )
    try:
        relative = _safe_output_path(output_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        items = manifest.get("items") if isinstance(manifest, dict) else None
        if not isinstance(items, list):
            raise ValueError("manifest items must be a JSON array")
        matches = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("status") == "exported"
            and item.get("output_path") == relative.as_posix()
        ]
        if len(matches) != 1:
            raise ValueError("output path must match exactly one exported manifest item")
        item = matches[0]
        target = root.joinpath(*relative.parts)
        resolved = target.resolve(strict=True)
        if target.is_symlink() or not resolved.is_file() or not _is_within(resolved, root):
            raise ValueError("exported script is not a regular in-workspace file")
        size_bytes = resolved.stat().st_size
        if size_bytes > MAX_SCRIPT_BYTES:
            return _parse_error_result(
                "limit_exceeded",
                "script exceeds the parser size limit",
                output_path=relative.as_posix(),
                source_size_bytes=size_bytes,
            )
        if size_bytes != item.get("decoded_size"):
            return _parse_error_result(
                "size_mismatch",
                "script size does not match the Phase 1 manifest",
                archive_name=item.get("archive_name", ""),
                internal_path=item.get("internal_path", ""),
                output_path=relative.as_posix(),
                entry_index=item.get("entry_index"),
                conflict_group=item.get("conflict_group"),
                source_size_bytes=size_bytes,
            )
        data = resolved.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return _parse_error_result(
            "invalid_input",
            f"exported script selection failed: {exc}",
            output_path=str(output_path),
        )
    return parse_qlie_script_bytes(
        data,
        archive_name=item.get("archive_name", ""),
        internal_path=item.get("internal_path", ""),
        output_path=relative.as_posix(),
        entry_index=item.get("entry_index"),
        conflict_group=item.get("conflict_group"),
        expected_sha256=item.get("decoded_sha256"),
    )


def render_script_parse_text(result):
    summary = result.to_dict()["summary"]
    kinds = ", ".join(
        f"{name}={count}" for name, count in summary["kind_counts"].items()
    )
    lines = [
        f"QLIE script parse: {result.status}",
        f"output_path: {result.output_path}",
        f"encoding: {result.encoding or 'unknown'}",
        (
            f"lines: {summary['nonblank_line_count']}/{summary['line_count']} nonblank; "
            f"segments: {summary['segment_count']}; "
            f"translatable: {summary['translatable_count']}"
        ),
        f"kinds: {kinds or 'none'}",
        f"reason: {result.reason}",
    ]
    lines.extend(f"warning: {warning}" for warning in result.warnings[:10])
    if len(result.warnings) > 10:
        lines.append(f"warning: {len(result.warnings) - 10} more warnings omitted")
    return "\n".join(lines)
