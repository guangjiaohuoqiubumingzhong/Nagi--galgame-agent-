"""Engine-independent contracts for Nagi's retrieval baseline."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..gameio.segments import TextSegment


RAG_SCHEMA_VERSION = 1
KEYWORD_INDEX_VERSION = 1
TOKENIZER_VERSION = 1


@dataclass(frozen=True)
class KeywordDocument:
    doc_id: int
    segment_id: str
    kind: str
    translatable: bool
    normalized_text: str
    speaker: str | None
    scene: str | None
    archive_name: str
    internal_path: str
    output_path: str
    source_sha256: str
    byte_start: int
    byte_end: int
    token_count: int

    @classmethod
    def from_segment(cls, doc_id, segment: TextSegment, token_count):
        return cls(
            doc_id=doc_id,
            segment_id=segment.segment_id,
            kind=segment.kind,
            translatable=segment.translatable,
            normalized_text=segment.normalized_text,
            speaker=segment.speaker,
            scene=segment.scene,
            archive_name=segment.source.archive_name,
            internal_path=segment.source.internal_path,
            output_path=segment.source.output_path,
            source_sha256=segment.source.source_sha256,
            byte_start=segment.source.byte_start,
            byte_end=segment.source.byte_end,
            token_count=token_count,
        )

    def to_dict(self):
        return {
            "doc_id": self.doc_id,
            "segment_id": self.segment_id,
            "kind": self.kind,
            "translatable": self.translatable,
            "normalized_text": self.normalized_text,
            "speaker": self.speaker,
            "scene": self.scene,
            "archive_name": self.archive_name,
            "internal_path": self.internal_path,
            "output_path": self.output_path,
            "source_sha256": self.source_sha256,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "token_count": self.token_count,
        }

    @classmethod
    def from_dict(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError("keyword document must be a JSON object")
        expected = {
            "doc_id",
            "segment_id",
            "kind",
            "translatable",
            "normalized_text",
            "speaker",
            "scene",
            "archive_name",
            "internal_path",
            "output_path",
            "source_sha256",
            "byte_start",
            "byte_end",
            "token_count",
        }
        if set(payload) != expected:
            raise ValueError("keyword document fields do not match index version 1")
        value = cls(**payload)
        if value.doc_id < 0 or value.token_count < 0:
            raise ValueError("keyword document counters must be non-negative")
        return value


@dataclass(frozen=True)
class KeywordQuery:
    text: str
    limit: int = 10
    kinds: tuple[str, ...] = ()
    speaker: str | None = None
    scene: str | None = None
    archive_name: str | None = None
    output_path_prefix: str | None = None
    translatable: bool | None = True


@dataclass(frozen=True)
class KeywordSearchResult:
    rank: int
    score: float
    matched_tokens: tuple[str, ...]
    document: KeywordDocument

    def to_dict(self, *, include_text=True):
        document = self.document.to_dict()
        if not include_text:
            document.pop("normalized_text")
        return {
            "rank": self.rank,
            "score": round(self.score, 8),
            "matched_tokens": list(self.matched_tokens),
            "document": document,
        }


@dataclass(frozen=True)
class KeywordSearchResponse:
    schema_version: int
    index_id: str
    query: KeywordQuery
    query_tokens: tuple[str, ...]
    results: tuple[KeywordSearchResult, ...]
    elapsed_ms: float

    def to_dict(self, *, include_text=True):
        return {
            "schema_version": self.schema_version,
            "index_id": self.index_id,
            "query": {
                "text": self.query.text,
                "limit": self.query.limit,
                "kinds": list(self.query.kinds),
                "speaker": self.query.speaker,
                "scene": self.query.scene,
                "archive_name": self.query.archive_name,
                "output_path_prefix": self.query.output_path_prefix,
                "translatable": self.query.translatable,
            },
            "query_tokens": list(self.query_tokens),
            "summary": {
                "result_count": len(self.results),
                "elapsed_ms": round(self.elapsed_ms, 3),
            },
            "results": [
                result.to_dict(include_text=include_text) for result in self.results
            ],
        }

    def to_json(self, *, include_text=True):
        return json.dumps(
            self.to_dict(include_text=include_text),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
