"""Versioned, engine-independent contracts for translation-ready text segments."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import PurePosixPath


SEGMENT_SCHEMA_VERSION = 1
SEGMENT_ID_VERSION = 1
SEGMENT_ID_PREFIX = f"seg_v{SEGMENT_ID_VERSION}_"
SEGMENT_KINDS = frozenset(
    {
        "dialogue",
        "narration",
        "choice",
        "speaker_name",
        "label",
        "control",
        "comment",
        "metadata",
        "unknown",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SEGMENT_ID_RE = re.compile(rf"^{SEGMENT_ID_PREFIX}[0-9a-f]{{64}}$")
MAX_JSONL_LINE_BYTES = 4 * 1024 * 1024
MAX_JSONL_SEGMENTS = 1_000_000


class SegmentContractError(ValueError):
    """Raised when a segment violates the versioned data contract."""


def _require_exact_keys(payload, expected, label):
    if not isinstance(payload, dict):
        raise SegmentContractError(f"{label} must be a JSON object")
    missing = sorted(expected - set(payload))
    extra = sorted(set(payload) - expected)
    if missing:
        raise SegmentContractError(f"{label} is missing fields: {', '.join(missing)}")
    if extra:
        raise SegmentContractError(f"{label} has unknown fields: {', '.join(extra)}")


def _require_nonempty_string(value, field):
    if not isinstance(value, str) or not value:
        raise SegmentContractError(f"{field} must be a non-empty string")


def _require_optional_string(value, field):
    if value is not None and not isinstance(value, str):
        raise SegmentContractError(f"{field} must be a string or null")


def _require_sha256(value, field):
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise SegmentContractError(f"{field} must be a lowercase SHA-256 hex digest")


def _is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_identity_path(value):
    return unicodedata.normalize("NFC", value.replace("\\", "/"))


def _validate_relative_posix_path(value, field):
    _require_nonempty_string(value, field)
    if "\\" in value:
        raise SegmentContractError(f"{field} must use POSIX separators")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ":" in path.parts[0]
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SegmentContractError(f"{field} must be a safe relative path")


def _validate_internal_path(value):
    _require_nonempty_string(value, "source internal_path")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or ":" in path.parts[0]
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SegmentContractError("source internal_path must be a safe relative path")


def normalize_segment_text(text):
    """Apply only lossless structural normalization used by Segment v1."""

    if not isinstance(text, str):
        raise SegmentContractError("source_text must be a string")
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def segment_text_sha256(source_text):
    if not isinstance(source_text, str):
        raise SegmentContractError("source_text must be a string")
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SegmentInlineToken:
    """A source-text span that translation must preserve or handle specially."""

    kind: str
    raw: str
    char_start: int
    char_end: int

    def __post_init__(self):
        if not isinstance(self.kind, str) or not TOKEN_KIND_RE.fullmatch(self.kind):
            raise SegmentContractError("token kind must be a lowercase identifier")
        _require_nonempty_string(self.raw, "token raw")
        if not _is_integer(self.char_start) or not _is_integer(self.char_end):
            raise SegmentContractError("token character spans must be integers")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise SegmentContractError("token character span must be non-empty and half-open")

    def to_dict(self):
        return {
            "kind": self.kind,
            "raw": self.raw,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }

    @classmethod
    def from_dict(cls, payload):
        _require_exact_keys(
            payload,
            {"kind", "raw", "char_start", "char_end"},
            "inline token",
        )
        return cls(**payload)


@dataclass(frozen=True)
class SegmentSource:
    """Immutable origin and exact source span for one text segment."""

    engine: str
    archive_name: str
    internal_path: str
    output_path: str
    entry_index: int
    conflict_group: str | None
    source_sha256: str
    source_size_bytes: int
    encoding: str
    byte_start: int
    byte_end: int
    line_start: int
    line_end: int

    def __post_init__(self):
        _require_nonempty_string(self.engine, "source engine")
        _validate_relative_posix_path(self.archive_name, "source archive_name")
        _validate_internal_path(self.internal_path)
        _validate_relative_posix_path(self.output_path, "source output_path")
        _require_optional_string(self.conflict_group, "source conflict_group")
        if self.conflict_group is not None:
            _validate_relative_posix_path(self.conflict_group, "source conflict_group")
        _require_sha256(self.source_sha256, "source source_sha256")
        _require_nonempty_string(self.encoding, "source encoding")
        if not _is_integer(self.entry_index) or self.entry_index < 0:
            raise SegmentContractError("source entry_index must be zero or greater")
        if not _is_integer(self.source_size_bytes) or self.source_size_bytes < 0:
            raise SegmentContractError("source_size_bytes must be zero or greater")
        if not _is_integer(self.byte_start) or not _is_integer(self.byte_end):
            raise SegmentContractError("source byte spans must be integers")
        if self.byte_start < 0 or self.byte_end <= self.byte_start:
            raise SegmentContractError("source byte span must be non-empty and half-open")
        if self.byte_end > self.source_size_bytes:
            raise SegmentContractError("source byte span exceeds source_size_bytes")
        if not _is_integer(self.line_start) or not _is_integer(self.line_end):
            raise SegmentContractError("source line spans must be integers")
        if self.line_start < 1 or self.line_end < self.line_start:
            raise SegmentContractError("source line span must be one-based and inclusive")

    def identity_dict(self):
        """Return only relocation-stable identity fields used by segment IDs."""

        return {
            "engine": self.engine,
            "archive_name": _normalize_identity_path(self.archive_name),
            "internal_path": _normalize_identity_path(self.internal_path),
            "entry_index": self.entry_index,
            "source_sha256": self.source_sha256,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
        }

    def file_identity(self):
        return (
            self.engine,
            _normalize_identity_path(self.archive_name),
            _normalize_identity_path(self.internal_path),
            self.entry_index,
            self.source_sha256,
        )

    def to_dict(self):
        return {
            "engine": self.engine,
            "archive_name": self.archive_name,
            "internal_path": self.internal_path,
            "output_path": self.output_path,
            "entry_index": self.entry_index,
            "conflict_group": self.conflict_group,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "encoding": self.encoding,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "line_start": self.line_start,
            "line_end": self.line_end,
        }

    @classmethod
    def from_dict(cls, payload):
        _require_exact_keys(
            payload,
            {
                "engine",
                "archive_name",
                "internal_path",
                "output_path",
                "entry_index",
                "conflict_group",
                "source_sha256",
                "source_size_bytes",
                "encoding",
                "byte_start",
                "byte_end",
                "line_start",
                "line_end",
            },
            "segment source",
        )
        return cls(**payload)


def make_segment_id(source, source_text_hash):
    if not isinstance(source, SegmentSource):
        raise SegmentContractError("source must be a SegmentSource")
    _require_sha256(source_text_hash, "source_text_sha256")
    identity = {
        "id_version": SEGMENT_ID_VERSION,
        "source": source.identity_dict(),
        "source_text_sha256": source_text_hash,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return SEGMENT_ID_PREFIX + hashlib.sha256(canonical).hexdigest()


def _validate_tokens(tokens, source_text, field):
    if not isinstance(tokens, tuple):
        raise SegmentContractError(f"{field} must be a tuple")
    previous_key = None
    seen = set()
    for token in tokens:
        if not isinstance(token, SegmentInlineToken):
            raise SegmentContractError(f"{field} must contain SegmentInlineToken values")
        if token.char_end > len(source_text):
            raise SegmentContractError(f"{field} token exceeds source_text")
        if source_text[token.char_start : token.char_end] != token.raw:
            raise SegmentContractError(f"{field} token raw text does not match source_text")
        key = (token.char_start, token.char_end, token.kind, token.raw)
        if previous_key is not None and key < previous_key:
            raise SegmentContractError(f"{field} tokens must use stable source order")
        if key in seen:
            raise SegmentContractError(f"{field} contains a duplicate token")
        seen.add(key)
        previous_key = key


@dataclass(frozen=True)
class TextSegment:
    """One versioned, traceable unit consumed by RAG and translation layers."""

    schema_version: int
    segment_id: str
    kind: str
    translatable: bool
    source_text: str
    normalized_text: str
    source_text_sha256: str
    speaker: str | None
    scene: str | None
    placeholders: tuple[SegmentInlineToken, ...]
    tags: tuple[SegmentInlineToken, ...]
    previous_segment_id: str | None
    next_segment_id: str | None
    source: SegmentSource
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        if self.schema_version != SEGMENT_SCHEMA_VERSION:
            raise SegmentContractError(
                f"unsupported segment schema_version: {self.schema_version}"
            )
        if not isinstance(self.kind, str) or self.kind not in SEGMENT_KINDS:
            raise SegmentContractError(f"unsupported segment kind: {self.kind}")
        if not isinstance(self.translatable, bool):
            raise SegmentContractError("translatable must be a boolean")
        _require_nonempty_string(self.source_text, "source_text")
        if not isinstance(self.normalized_text, str):
            raise SegmentContractError("normalized_text must be a string")
        _require_sha256(self.source_text_sha256, "source_text_sha256")
        _require_optional_string(self.speaker, "speaker")
        _require_optional_string(self.scene, "scene")
        _require_optional_string(self.previous_segment_id, "previous_segment_id")
        _require_optional_string(self.next_segment_id, "next_segment_id")
        if not isinstance(self.source, SegmentSource):
            raise SegmentContractError("source must be a SegmentSource")
        if not isinstance(self.warnings, tuple) or any(
            not isinstance(warning, str) for warning in self.warnings
        ):
            raise SegmentContractError("warnings must be a tuple of strings")
        expected_text_hash = segment_text_sha256(self.source_text)
        if self.source_text_sha256 != expected_text_hash:
            raise SegmentContractError("source_text_sha256 does not match source_text")
        expected_id = make_segment_id(self.source, self.source_text_sha256)
        if self.segment_id != expected_id:
            raise SegmentContractError("segment_id does not match Segment v1 identity")
        _validate_tokens(self.placeholders, self.source_text, "placeholders")
        _validate_tokens(self.tags, self.source_text, "tags")
        for field, value in (
            ("previous_segment_id", self.previous_segment_id),
            ("next_segment_id", self.next_segment_id),
        ):
            if value is not None and not SEGMENT_ID_RE.fullmatch(value):
                raise SegmentContractError(f"{field} is not a Segment v1 ID")

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "segment_id": self.segment_id,
            "kind": self.kind,
            "translatable": self.translatable,
            "source_text": self.source_text,
            "normalized_text": self.normalized_text,
            "source_text_sha256": self.source_text_sha256,
            "speaker": self.speaker,
            "scene": self.scene,
            "placeholders": [token.to_dict() for token in self.placeholders],
            "tags": [token.to_dict() for token in self.tags],
            "previous_segment_id": self.previous_segment_id,
            "next_segment_id": self.next_segment_id,
            "source": self.source.to_dict(),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, payload):
        expected = {
            "schema_version",
            "segment_id",
            "kind",
            "translatable",
            "source_text",
            "normalized_text",
            "source_text_sha256",
            "speaker",
            "scene",
            "placeholders",
            "tags",
            "previous_segment_id",
            "next_segment_id",
            "source",
            "warnings",
        }
        _require_exact_keys(payload, expected, "text segment")
        placeholders = payload["placeholders"]
        tags = payload["tags"]
        warnings = payload["warnings"]
        if not isinstance(placeholders, list) or not isinstance(tags, list):
            raise SegmentContractError("placeholders and tags must be JSON arrays")
        if not isinstance(warnings, list):
            raise SegmentContractError("warnings must be a JSON array")
        values = dict(payload)
        values["placeholders"] = tuple(
            SegmentInlineToken.from_dict(token) for token in placeholders
        )
        values["tags"] = tuple(SegmentInlineToken.from_dict(token) for token in tags)
        values["source"] = SegmentSource.from_dict(payload["source"])
        values["warnings"] = tuple(warnings)
        return cls(**values)


def build_text_segment(
    *,
    kind,
    source,
    source_text,
    translatable,
    normalized_text=None,
    speaker=None,
    scene=None,
    placeholders=(),
    tags=(),
    warnings=(),
):
    """Build a validated Segment v1 value and calculate its stable identity."""

    text_hash = segment_text_sha256(source_text)
    return TextSegment(
        schema_version=SEGMENT_SCHEMA_VERSION,
        segment_id=make_segment_id(source, text_hash),
        kind=kind,
        translatable=translatable,
        source_text=source_text,
        normalized_text=(
            normalize_segment_text(source_text)
            if normalized_text is None
            else normalized_text
        ),
        source_text_sha256=text_hash,
        speaker=speaker,
        scene=scene,
        placeholders=tuple(placeholders),
        tags=tuple(tags),
        previous_segment_id=None,
        next_segment_id=None,
        source=source,
        warnings=tuple(warnings),
    )


def link_segment_sequence(segments):
    """Attach reciprocal previous/next IDs to one ordered source-file sequence."""

    values = tuple(segments)
    if not values:
        return ()
    if any(not isinstance(segment, TextSegment) for segment in values):
        raise SegmentContractError("segment sequence must contain TextSegment values")
    identity = values[0].source.file_identity()
    previous_end = -1
    for segment in values:
        if segment.source.file_identity() != identity:
            raise SegmentContractError("segment sequence must belong to one source file")
        if segment.source.byte_start < previous_end:
            raise SegmentContractError("segment sequence byte spans overlap or are out of order")
        previous_end = segment.source.byte_end
    return tuple(
        replace(
            segment,
            previous_segment_id=values[index - 1].segment_id if index else None,
            next_segment_id=(
                values[index + 1].segment_id if index + 1 < len(values) else None
            ),
        )
        for index, segment in enumerate(values)
    )


def validate_segment_collection(segments):
    values = tuple(segments)
    if any(not isinstance(segment, TextSegment) for segment in values):
        raise SegmentContractError("segment collection must contain TextSegment values")
    ids = [segment.segment_id for segment in values]
    if len(ids) != len(set(ids)):
        raise SegmentContractError("segment collection contains duplicate segment IDs")
    by_id = {segment.segment_id: segment for segment in values}
    for segment in values:
        if segment.previous_segment_id is not None:
            previous = by_id.get(segment.previous_segment_id)
            if previous is None or previous.next_segment_id != segment.segment_id:
                raise SegmentContractError("previous_segment_id is missing or not reciprocal")
        if segment.next_segment_id is not None:
            following = by_id.get(segment.next_segment_id)
            if following is None or following.previous_segment_id != segment.segment_id:
                raise SegmentContractError("next_segment_id is missing or not reciprocal")
    return values


def segments_to_jsonl(segments):
    """Serialize a self-contained segment collection deterministically."""

    values = validate_segment_collection(segments)
    if not values:
        return ""
    return "\n".join(
        json.dumps(
            segment.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for segment in values
    ) + "\n"


def segments_from_jsonl(text):
    """Parse and strictly validate a self-contained Segment v1 JSONL string."""

    if not isinstance(text, str):
        raise SegmentContractError("segments JSONL input must be a string")
    if not text:
        return ()
    lines = text.splitlines()
    if len(lines) > MAX_JSONL_SEGMENTS:
        raise SegmentContractError("segments JSONL exceeds the segment count limit")
    values = []
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise SegmentContractError(f"segments JSONL line {line_number} is blank")
        if len(line.encode("utf-8")) > MAX_JSONL_LINE_BYTES:
            raise SegmentContractError(
                f"segments JSONL line {line_number} exceeds the size limit"
            )
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SegmentContractError(
                f"segments JSONL line {line_number} is invalid JSON"
            ) from exc
        values.append(TextSegment.from_dict(payload))
    return validate_segment_collection(values)
