"""Deterministic offline vector retrieval using stable feature hashing."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from .index import LoadedKeywordIndex
from .ingest import tokenize_keyword_text
from .models import KeywordDocument, KeywordQuery, RAG_SCHEMA_VERSION
from .retriever import MAX_QUERY_LIMIT, matches_query_filters


VECTOR_INDEX_VERSION = 1
VECTOR_MODEL_ID = "feature-hashing-word-cjk-v1"
VECTOR_DIMENSIONS = 1024
VECTOR_NORMALIZATION = "l2"


SparseVector = tuple[tuple[int, float], ...]


def _canonical_sha256(payload):
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hashed_coordinates(text):
    counts = Counter(tokenize_keyword_text(text))
    coordinates = defaultdict(float)
    for token, frequency in sorted(counts.items()):
        digest = hashlib.sha256((VECTOR_MODEL_ID + "\0" + token).encode("utf-8")).digest()
        dimension = int.from_bytes(digest[:8], "big") % VECTOR_DIMENSIONS
        sign = 1.0 if digest[8] & 1 == 0 else -1.0
        coordinates[dimension] += sign * (1.0 + math.log(frequency))
    norm = math.sqrt(sum(value * value for value in coordinates.values()))
    if norm == 0.0:
        return ()
    return tuple(
        (dimension, value / norm)
        for dimension, value in sorted(coordinates.items())
        if value != 0.0
    )


def _dot(left, right):
    left_values = dict(left)
    return sum(left_values.get(dimension, 0.0) * value for dimension, value in right)


@dataclass(frozen=True)
class LoadedVectorIndex:
    index_id: str
    source_index_id: str
    model_id: str
    dimensions: int
    normalization: str
    documents: tuple[KeywordDocument, ...]
    vectors: tuple[SparseVector, ...]


@dataclass(frozen=True)
class VectorSearchResult:
    rank: int
    score: float
    document: KeywordDocument


@dataclass(frozen=True)
class VectorSearchResponse:
    schema_version: int
    index_id: str
    model_id: str
    dimensions: int
    normalization: str
    query: KeywordQuery
    results: tuple[VectorSearchResult, ...]
    elapsed_ms: float


def build_vector_index(keyword_index):
    """Build an in-memory vector index from the exact keyword-index documents."""
    if not isinstance(keyword_index, LoadedKeywordIndex):
        raise TypeError("keyword_index must be a LoadedKeywordIndex")
    identity = {
        "index_version": VECTOR_INDEX_VERSION,
        "source_index_id": keyword_index.index_id,
        "model_id": VECTOR_MODEL_ID,
        "dimensions": VECTOR_DIMENSIONS,
        "normalization": VECTOR_NORMALIZATION,
    }
    return LoadedVectorIndex(
        index_id="vec_v1_" + _canonical_sha256(identity),
        source_index_id=keyword_index.index_id,
        model_id=VECTOR_MODEL_ID,
        dimensions=VECTOR_DIMENSIONS,
        normalization=VECTOR_NORMALIZATION,
        documents=keyword_index.documents,
        vectors=tuple(
            _hashed_coordinates(document.normalized_text)
            for document in keyword_index.documents
        ),
    )


def search_vector_index(index, query):
    if not isinstance(index, LoadedVectorIndex):
        raise TypeError("index must be a LoadedVectorIndex")
    if not isinstance(query, KeywordQuery):
        raise TypeError("query must be a KeywordQuery")
    if not query.text.strip():
        raise ValueError("vector query text cannot be empty")
    if isinstance(query.limit, bool) or not 1 <= query.limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"vector query limit must be between 1 and {MAX_QUERY_LIMIT}")
    started = time.perf_counter()
    query_vector = _hashed_coordinates(query.text)
    scored = []
    if query_vector:
        for document, vector in zip(index.documents, index.vectors):
            if not matches_query_filters(document, query):
                continue
            score = _dot(query_vector, vector)
            if score > 0.0:
                scored.append((score, document))
    scored.sort(key=lambda item: (-item[0], item[1].segment_id))
    results = tuple(
        VectorSearchResult(rank=rank, score=score, document=document)
        for rank, (score, document) in enumerate(scored[: query.limit], start=1)
    )
    return VectorSearchResponse(
        schema_version=RAG_SCHEMA_VERSION,
        index_id=index.index_id,
        model_id=index.model_id,
        dimensions=index.dimensions,
        normalization=index.normalization,
        query=query,
        results=results,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


__all__ = [
    "LoadedVectorIndex",
    "VECTOR_DIMENSIONS",
    "VECTOR_INDEX_VERSION",
    "VECTOR_MODEL_ID",
    "VECTOR_NORMALIZATION",
    "VectorSearchResponse",
    "VectorSearchResult",
    "build_vector_index",
    "search_vector_index",
]
