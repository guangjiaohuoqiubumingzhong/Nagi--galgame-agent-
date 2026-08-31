"""Deterministic BM25 retrieval over a loaded local keyword index."""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict

from .index import LoadedKeywordIndex
from .ingest import normalize_keyword_text, tokenize_keyword_text
from .models import (
    RAG_SCHEMA_VERSION,
    KeywordQuery,
    KeywordSearchResponse,
    KeywordSearchResult,
)


BM25_K1 = 1.2
BM25_B = 0.75
MAX_QUERY_LIMIT = 100


def _normalized_equal(left, right):
    return normalize_keyword_text(left) == normalize_keyword_text(right)


def matches_query_filters(document, query):
    """Apply the shared metadata-filter contract used by every retrieval backend."""
    if query.kinds and document.kind not in query.kinds:
        return False
    if query.speaker is not None and (
        document.speaker is None or not _normalized_equal(document.speaker, query.speaker)
    ):
        return False
    if query.scene is not None and (
        document.scene is None or not _normalized_equal(document.scene, query.scene)
    ):
        return False
    if query.archive_name is not None and document.archive_name.casefold() != query.archive_name.casefold():
        return False
    if query.output_path_prefix is not None:
        prefix = query.output_path_prefix.replace("\\", "/").casefold().rstrip("/")
        output_path = document.output_path.replace("\\", "/").casefold()
        if output_path != prefix and not output_path.startswith(prefix + "/"):
            return False
    if query.translatable is not None and document.translatable != query.translatable:
        return False
    return True


def search_keyword_index(index, query):
    if not isinstance(index, LoadedKeywordIndex):
        raise TypeError("index must be a LoadedKeywordIndex")
    if not isinstance(query, KeywordQuery):
        raise TypeError("query must be a KeywordQuery")
    if not query.text.strip():
        raise ValueError("keyword query text cannot be empty")
    if isinstance(query.limit, bool) or not 1 <= query.limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"keyword query limit must be between 1 and {MAX_QUERY_LIMIT}")
    started = time.perf_counter()
    query_counts = Counter(tokenize_keyword_text(query.text))
    if not query_counts:
        return KeywordSearchResponse(
            schema_version=RAG_SCHEMA_VERSION,
            index_id=index.index_id,
            query=query,
            query_tokens=(),
            results=(),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    document_count = len(index.documents)
    average_length = index.average_document_length or 1.0
    scores = defaultdict(float)
    matched = defaultdict(set)
    for token, query_frequency in sorted(query_counts.items()):
        postings = index.postings.get(token, ())
        document_frequency = len(postings)
        if not document_frequency:
            continue
        inverse_document_frequency = math.log(
            1.0
            + (document_count - document_frequency + 0.5)
            / (document_frequency + 0.5)
        )
        query_weight = 1.0 + math.log(query_frequency)
        for doc_id, term_frequency in postings:
            document = index.documents[doc_id]
            if not matches_query_filters(document, query):
                continue
            normalization = BM25_K1 * (
                1.0 - BM25_B
                + BM25_B * document.token_count / average_length
            )
            score = inverse_document_frequency * (
                term_frequency * (BM25_K1 + 1.0)
                / (term_frequency + normalization)
            )
            scores[doc_id] += query_weight * score
            matched[doc_id].add(token)
    ranked = sorted(
        scores,
        key=lambda doc_id: (-scores[doc_id], index.documents[doc_id].segment_id),
    )[: query.limit]
    results = tuple(
        KeywordSearchResult(
            rank=rank,
            score=scores[doc_id],
            matched_tokens=tuple(
                token[2:] if len(token) > 2 and token[1] == ":" else token
                for token in sorted(matched[doc_id])
            ),
            document=index.documents[doc_id],
        )
        for rank, doc_id in enumerate(ranked, start=1)
    )
    return KeywordSearchResponse(
        schema_version=RAG_SCHEMA_VERSION,
        index_id=index.index_id,
        query=query,
        query_tokens=tuple(sorted(query_counts)),
        results=results,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
