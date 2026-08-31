"""Deterministic reciprocal-rank fusion over keyword and vector retrieval."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace

from .index import LoadedKeywordIndex
from .models import KeywordDocument, KeywordQuery, RAG_SCHEMA_VERSION
from .retriever import MAX_QUERY_LIMIT, search_keyword_index
from .vector import LoadedVectorIndex, search_vector_index


HYBRID_INDEX_VERSION = 1
HYBRID_FUSION_ID = "rrf-v1"
RRF_K = 60
HYBRID_CANDIDATE_MULTIPLIER = 4


def _canonical_sha256(payload):
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class HybridSearchResult:
    rank: int
    score: float
    keyword_rank: int | None
    vector_rank: int | None
    document: KeywordDocument

    @property
    def contributing_routes(self):
        return tuple(
            route
            for route, rank in (("keyword", self.keyword_rank), ("vector", self.vector_rank))
            if rank is not None
        )


@dataclass(frozen=True)
class HybridSearchResponse:
    schema_version: int
    index_id: str
    fusion_id: str
    rrf_k: int
    keyword_index_id: str
    vector_index_id: str
    query: KeywordQuery
    results: tuple[HybridSearchResult, ...]
    elapsed_ms: float


def hybrid_index_id(keyword_index, vector_index):
    if vector_index.source_index_id != keyword_index.index_id:
        raise ValueError("hybrid indexes must describe the same document corpus")
    identity = {
        "index_version": HYBRID_INDEX_VERSION,
        "fusion_id": HYBRID_FUSION_ID,
        "rrf_k": RRF_K,
        "candidate_multiplier": HYBRID_CANDIDATE_MULTIPLIER,
        "keyword_index_id": keyword_index.index_id,
        "vector_index_id": vector_index.index_id,
    }
    return "hybrid_v1_" + _canonical_sha256(identity)


def search_hybrid_index(keyword_index, vector_index, query):
    if not isinstance(keyword_index, LoadedKeywordIndex):
        raise TypeError("keyword_index must be a LoadedKeywordIndex")
    if not isinstance(vector_index, LoadedVectorIndex):
        raise TypeError("vector_index must be a LoadedVectorIndex")
    if not isinstance(query, KeywordQuery):
        raise TypeError("query must be a KeywordQuery")
    if vector_index.source_index_id != keyword_index.index_id:
        raise ValueError("hybrid indexes must describe the same document corpus")
    started = time.perf_counter()
    candidate_limit = min(MAX_QUERY_LIMIT, max(query.limit, query.limit * HYBRID_CANDIDATE_MULTIPLIER))
    candidate_query = replace(query, limit=candidate_limit)
    keyword_response = search_keyword_index(keyword_index, candidate_query)
    vector_response = search_vector_index(vector_index, candidate_query)

    candidates = {}
    for route, results in (
        ("keyword", keyword_response.results),
        ("vector", vector_response.results),
    ):
        for result in results:
            row = candidates.setdefault(
                result.document.segment_id,
                {"document": result.document, "keyword_rank": None, "vector_rank": None},
            )
            row[f"{route}_rank"] = result.rank

    def fused_score(row):
        return sum(
            1.0 / (RRF_K + rank)
            for rank in (row["keyword_rank"], row["vector_rank"])
            if rank is not None
        )

    ranked = sorted(
        candidates.values(),
        key=lambda row: (-fused_score(row), row["document"].segment_id),
    )[: query.limit]
    results = tuple(
        HybridSearchResult(
            rank=rank,
            score=fused_score(row),
            keyword_rank=row["keyword_rank"],
            vector_rank=row["vector_rank"],
            document=row["document"],
        )
        for rank, row in enumerate(ranked, start=1)
    )
    return HybridSearchResponse(
        schema_version=RAG_SCHEMA_VERSION,
        index_id=hybrid_index_id(keyword_index, vector_index),
        fusion_id=HYBRID_FUSION_ID,
        rrf_k=RRF_K,
        keyword_index_id=keyword_index.index_id,
        vector_index_id=vector_index.index_id,
        query=query,
        results=results,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


__all__ = [
    "HYBRID_CANDIDATE_MULTIPLIER",
    "HYBRID_FUSION_ID",
    "HybridSearchResponse",
    "HybridSearchResult",
    "RRF_K",
    "hybrid_index_id",
    "search_hybrid_index",
]
