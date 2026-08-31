"""Engine-independent contracts for planned and generated translations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from ..gameio.segments import SegmentInlineToken, TextSegment
from ..messages import MESSAGE_FORMAT

TRANSLATION_SCHEMA_VERSION = 1
TRANSLATION_PLANNER_VERSION = 1
TRANSLATION_CACHE_VERSION = 1
UNIT_ID_PREFIX = "tu_v1_"
BATCH_ID_PREFIX = "tb_v1_"
PLAN_ID_PREFIX = "tp_v1_"
CANDIDATE_ID_PREFIX = "tc_v1_"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BATCH_STATUSES = frozenset({"planned", "running", "partial", "completed", "failed"})
CANDIDATE_STATUSES = frozenset({"generated", "rejected", "accepted"})


def _canonical_sha256(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nonempty(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _sha256(value, field_name):
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True)
class TranslationSourceReference:
    engine: str
    archive_name: str
    internal_path: str
    output_path: str
    encoding: str
    entry_index: int
    source_sha256: str
    source_text_sha256: str
    byte_start: int
    byte_end: int
    line_start: int
    line_end: int

    @classmethod
    def from_segment(cls, segment):
        if not isinstance(segment, TextSegment):
            raise TypeError("segment must be a TextSegment")
        source = segment.source
        return cls(
            engine=source.engine,
            archive_name=source.archive_name,
            internal_path=source.internal_path,
            output_path=source.output_path,
            encoding=source.encoding,
            entry_index=source.entry_index,
            source_sha256=source.source_sha256,
            source_text_sha256=segment.source_text_sha256,
            byte_start=source.byte_start,
            byte_end=source.byte_end,
            line_start=source.line_start,
            line_end=source.line_end,
        )

    def __post_init__(self):
        for field_name in ("engine", "archive_name", "internal_path", "output_path", "encoding"):
            _nonempty(getattr(self, field_name), f"source {field_name}")
        _sha256(self.source_sha256, "source source_sha256")
        _sha256(self.source_text_sha256, "source source_text_sha256")
        if isinstance(self.entry_index, bool) or not isinstance(self.entry_index, int) or self.entry_index < 0:
            raise ValueError("source entry_index must be zero or greater")
        if self.byte_start < 0 or self.byte_end <= self.byte_start:
            raise ValueError("source byte span must be non-empty and half-open")
        if self.line_start < 1 or self.line_end < self.line_start:
            raise ValueError("source line span must be one-based and inclusive")

    def to_dict(self):
        return {
            "engine": self.engine,
            "archive_name": self.archive_name,
            "internal_path": self.internal_path,
            "output_path": self.output_path,
            "encoding": self.encoding,
            "entry_index": self.entry_index,
            "source_sha256": self.source_sha256,
            "source_text_sha256": self.source_text_sha256,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "line_start": self.line_start,
            "line_end": self.line_end,
        }


@dataclass(frozen=True)
class RAGEvidenceReference:
    index_id: str
    segment_id: str
    source: str
    score: float
    content_sha256: str

    def __post_init__(self):
        _nonempty(self.index_id, "RAG index_id")
        _nonempty(self.segment_id, "RAG segment_id")
        _nonempty(self.source, "RAG source")
        _sha256(self.content_sha256, "RAG content_sha256")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError("RAG score must be numeric")
        if not math.isfinite(float(self.score)):
            raise ValueError("RAG score must be finite")

    def identity_dict(self):
        return {
            "index_id": self.index_id,
            "segment_id": self.segment_id,
            "source": self.source,
            "score": round(float(self.score), 8),
            "content_sha256": self.content_sha256,
        }

    def to_dict(self):
        return self.identity_dict()


def make_translation_unit_id(segment):
    if not isinstance(segment, TextSegment):
        raise TypeError("segment must be a TextSegment")
    return UNIT_ID_PREFIX + _canonical_sha256(
        {
            "schema_version": TRANSLATION_SCHEMA_VERSION,
            "segment_id": segment.segment_id,
            "source_text_sha256": segment.source_text_sha256,
        }
    )


def make_translation_cache_key(
    *,
    unit_id,
    source_text_sha256,
    model_id,
    prompt_version,
    terminology_version,
    rag_index_id,
    rag_evidence=(),
):
    evidence = tuple(rag_evidence)
    if any(not isinstance(item, RAGEvidenceReference) for item in evidence):
        raise TypeError("rag_evidence must contain RAGEvidenceReference values")
    return "tcache_v1_" + _canonical_sha256(
        {
            "cache_version": TRANSLATION_CACHE_VERSION,
            "message_format": MESSAGE_FORMAT,
            "unit_id": _nonempty(unit_id, "unit_id"),
            "source_text_sha256": _sha256(source_text_sha256, "source_text_sha256"),
            "model_id": _nonempty(model_id, "model_id"),
            "prompt_version": _nonempty(prompt_version, "prompt_version"),
            "terminology_version": _nonempty(terminology_version, "terminology_version"),
            "rag_index_id": _nonempty(rag_index_id, "rag_index_id"),
            "rag_evidence": [item.identity_dict() for item in evidence],
        }
    )


@dataclass(frozen=True)
class TranslationUnit:
    unit_id: str
    segment_id: str
    cache_key: str
    kind: str
    speaker: str | None
    scene: str | None
    source_text: str = field(repr=False)
    normalized_text: str = field(repr=False)
    source_text_sha256: str
    source: TranslationSourceReference
    placeholders: tuple[SegmentInlineToken, ...] = field(default=(), repr=False)
    tags: tuple[SegmentInlineToken, ...] = field(default=(), repr=False)
    previous_segment_id: str | None = None
    next_segment_id: str | None = None
    rag_evidence: tuple[RAGEvidenceReference, ...] = ()

    def __post_init__(self):
        _nonempty(self.segment_id, "segment_id")
        expected_unit_id = UNIT_ID_PREFIX + _canonical_sha256(
            {
                "schema_version": TRANSLATION_SCHEMA_VERSION,
                "segment_id": self.segment_id,
                "source_text_sha256": self.source_text_sha256,
            }
        )
        if self.unit_id != expected_unit_id:
            raise ValueError("translation unit_id does not match source identity")
        if not self.cache_key.startswith("tcache_v1_"):
            raise ValueError("translation cache_key has an unsupported version")
        _nonempty(self.kind, "kind")
        if not isinstance(self.source_text, str) or not self.source_text:
            raise ValueError("source_text must be non-empty")
        if not isinstance(self.normalized_text, str):
            raise ValueError("normalized_text must be a string")
        _sha256(self.source_text_sha256, "source_text_sha256")
        if self.source is None or not isinstance(self.source, TranslationSourceReference):
            raise TypeError("source must be a TranslationSourceReference")
        if self.source.source_text_sha256 != self.source_text_sha256:
            raise ValueError("translation unit source text hashes do not match")
        if any(not isinstance(item, SegmentInlineToken) for item in self.placeholders + self.tags):
            raise TypeError("translation unit tokens must be SegmentInlineToken values")
        if any(not isinstance(item, RAGEvidenceReference) for item in self.rag_evidence):
            raise TypeError("rag_evidence must contain RAGEvidenceReference values")

    def to_dict(self, *, include_text=False):
        payload = {
            "unit_id": self.unit_id,
            "segment_id": self.segment_id,
            "cache_key": self.cache_key,
            "kind": self.kind,
            "speaker": self.speaker,
            "scene": self.scene,
            "source_text_sha256": self.source_text_sha256,
            "placeholder_count": len(self.placeholders),
            "tag_count": len(self.tags),
            "previous_segment_id": self.previous_segment_id,
            "next_segment_id": self.next_segment_id,
            "source": self.source.to_dict(),
            "rag_evidence": [item.to_dict() for item in self.rag_evidence],
        }
        if include_text:
            payload["source_text"] = self.source_text
            payload["normalized_text"] = self.normalized_text
            payload["placeholders"] = [item.to_dict() for item in self.placeholders]
            payload["tags"] = [item.to_dict() for item in self.tags]
        return payload


@dataclass(frozen=True)
class TranslationCandidate:
    candidate_id: str
    unit_id: str
    segment_id: str
    cache_key: str
    translated_text: str = field(repr=False)
    translated_text_sha256: str
    status: str
    model_id: str
    prompt_version: str
    terminology_version: str
    rag_index_id: str
    rag_evidence: tuple[RAGEvidenceReference, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        if self.status not in CANDIDATE_STATUSES:
            raise ValueError("unsupported translation candidate status")
        if not isinstance(self.translated_text, str) or not self.translated_text.strip():
            raise ValueError("translated_text must be non-empty")
        if self.translated_text_sha256 != hashlib.sha256(
            self.translated_text.encode("utf-8")
        ).hexdigest():
            raise ValueError("translated_text_sha256 does not match translated_text")
        for value, name in (
            (self.unit_id, "unit_id"),
            (self.segment_id, "segment_id"),
            (self.cache_key, "cache_key"),
            (self.model_id, "model_id"),
            (self.prompt_version, "prompt_version"),
            (self.terminology_version, "terminology_version"),
            (self.rag_index_id, "rag_index_id"),
        ):
            _nonempty(value, name)
        if any(not isinstance(item, RAGEvidenceReference) for item in self.rag_evidence):
            raise TypeError("rag_evidence must contain RAGEvidenceReference values")
        if any(not isinstance(item, str) for item in self.warnings):
            raise TypeError("warnings must contain strings")
        expected_candidate_id = CANDIDATE_ID_PREFIX + _canonical_sha256(
            {
                "schema_version": TRANSLATION_SCHEMA_VERSION,
                "unit_id": self.unit_id,
                "segment_id": self.segment_id,
                "cache_key": self.cache_key,
                "translated_text_sha256": self.translated_text_sha256,
                "model_id": self.model_id,
                "prompt_version": self.prompt_version,
                "terminology_version": self.terminology_version,
                "rag_index_id": self.rag_index_id,
                "rag_evidence": [item.identity_dict() for item in self.rag_evidence],
            }
        )
        if self.candidate_id != expected_candidate_id:
            raise ValueError("candidate_id does not match translation candidate identity")

    def to_dict(self, *, include_text=True):
        payload = {
            "candidate_id": self.candidate_id,
            "unit_id": self.unit_id,
            "segment_id": self.segment_id,
            "cache_key": self.cache_key,
            "translated_text_sha256": self.translated_text_sha256,
            "status": self.status,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "terminology_version": self.terminology_version,
            "rag_index_id": self.rag_index_id,
            "rag_evidence": [item.to_dict() for item in self.rag_evidence],
            "warnings": list(self.warnings),
        }
        if include_text:
            payload["translated_text"] = self.translated_text
        return payload


def build_translation_candidate(
    unit,
    translated_text,
    *,
    model_id,
    prompt_version,
    terminology_version,
    rag_index_id,
    status="generated",
    rag_evidence=None,
    warnings=(),
):
    if not isinstance(unit, TranslationUnit):
        raise TypeError("unit must be a TranslationUnit")
    evidence = unit.rag_evidence if rag_evidence is None else tuple(rag_evidence)
    if not isinstance(translated_text, str):
        raise TypeError("translated_text must be a string")
    if any(not isinstance(item, RAGEvidenceReference) for item in evidence):
        raise TypeError("rag_evidence must contain RAGEvidenceReference values")
    translated_hash = hashlib.sha256(translated_text.encode("utf-8")).hexdigest()
    cache_key = make_translation_cache_key(
        unit_id=unit.unit_id,
        source_text_sha256=unit.source_text_sha256,
        model_id=model_id,
        prompt_version=prompt_version,
        terminology_version=terminology_version,
        rag_index_id=rag_index_id,
        rag_evidence=evidence,
    )
    candidate_id = CANDIDATE_ID_PREFIX + _canonical_sha256(
        {
            "schema_version": TRANSLATION_SCHEMA_VERSION,
            "unit_id": unit.unit_id,
            "segment_id": unit.segment_id,
            "cache_key": cache_key,
            "translated_text_sha256": translated_hash,
            "model_id": model_id,
            "prompt_version": prompt_version,
            "terminology_version": terminology_version,
            "rag_index_id": rag_index_id,
            "rag_evidence": [item.identity_dict() for item in evidence],
        }
    )
    return TranslationCandidate(
        candidate_id=candidate_id,
        unit_id=unit.unit_id,
        segment_id=unit.segment_id,
        cache_key=cache_key,
        translated_text=translated_text,
        translated_text_sha256=translated_hash,
        status=status,
        model_id=model_id,
        prompt_version=prompt_version,
        terminology_version=terminology_version,
        rag_index_id=rag_index_id,
        rag_evidence=evidence,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class TranslationBatch:
    batch_id: str
    ordinal: int
    status: str
    units: tuple[TranslationUnit, ...] = field(repr=False)

    def __post_init__(self):
        if not self.batch_id.startswith(BATCH_ID_PREFIX):
            raise ValueError("batch_id has an unsupported version")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise ValueError("batch ordinal must be one or greater")
        if self.status not in BATCH_STATUSES:
            raise ValueError("unsupported translation batch status")
        if not self.units or any(not isinstance(unit, TranslationUnit) for unit in self.units):
            raise ValueError("translation batch must contain TranslationUnit values")

    def to_dict(self, *, include_text=False):
        return {
            "batch_id": self.batch_id,
            "ordinal": self.ordinal,
            "status": self.status,
            "unit_count": len(self.units),
            "units": [unit.to_dict(include_text=include_text) for unit in self.units],
        }
