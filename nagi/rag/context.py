"""Adapters that turn retrieval results into prompt-safe context candidates."""

from __future__ import annotations

from dataclasses import dataclass, field

from .index import LoadedKeywordIndex
from .models import KeywordQuery
from .retriever import search_keyword_index


@dataclass(frozen=True)
class KeywordContextRetriever:
    """Expose a loaded keyword index through ContextManager's small callback contract."""

    index: LoadedKeywordIndex
    filters: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.index, LoadedKeywordIndex):
            raise TypeError("index must be a LoadedKeywordIndex")
        allowed = {
            "kinds",
            "speaker",
            "scene",
            "archive_name",
            "output_path_prefix",
            "translatable",
        }
        unknown = sorted(set(self.filters) - allowed)
        if unknown:
            raise ValueError(f"unsupported keyword context filters: {', '.join(unknown)}")

    def __call__(self, text, *, limit=5):
        query = KeywordQuery(text=str(text), limit=int(limit), **dict(self.filters))
        response = search_keyword_index(self.index, query)
        candidates = []
        for result in response.results:
            document = result.document
            candidates.append(
                {
                    "text": document.normalized_text,
                    "source": (
                        f"{document.archive_name}:{document.output_path}:"
                        f"{document.byte_start}-{document.byte_end}"
                    ),
                    "segment_id": document.segment_id,
                    "index_id": response.index_id,
                    "kind": document.kind,
                    "speaker": document.speaker,
                    "scene": document.scene,
                    "score": result.score,
                }
            )
        return candidates
