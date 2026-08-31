from dataclasses import replace
from pathlib import Path

import nagi.rag.hybrid as hybrid_module
from nagi.rag import (
    KeywordQuery,
    build_vector_index,
    load_retrieval_benchmark,
    search_hybrid_index,
    search_vector_index,
)


RETRIEVAL_BENCHMARK = Path("benchmarks/qlie_retrieval_tasks.json")


def test_vector_index_identity_and_rankings_are_deterministic():
    benchmark = load_retrieval_benchmark(RETRIEVAL_BENCHMARK)
    first = build_vector_index(benchmark.index)
    second = build_vector_index(benchmark.index)
    query = KeywordQuery(text="silver key observatory", limit=3)

    first_response = search_vector_index(first, query)
    second_response = search_vector_index(second, query)

    assert first.index_id == second.index_id
    assert first.model_id == "feature-hashing-word-cjk-v1"
    assert first.dimensions == 1024
    assert first.normalization == "l2"
    assert [item.document.segment_id for item in first_response.results] == [
        item.document.segment_id for item in second_response.results
    ]
    assert [item.document.segment_id for item in first_response.results[:2]] == [
        "synthetic:silver-key-gate",
        "synthetic:key-history",
    ]


def test_vector_search_uses_the_shared_metadata_filters():
    benchmark = load_retrieval_benchmark(RETRIEVAL_BENCHMARK)
    vector = build_vector_index(benchmark.index)
    response = search_vector_index(
        vector,
        KeywordQuery(text="silver key", limit=2, scene="night"),
    )

    assert [item.document.segment_id for item in response.results] == [
        "synthetic:key-history"
    ]


def test_hybrid_executes_both_routes_and_exposes_route_ranks(monkeypatch):
    benchmark = load_retrieval_benchmark(RETRIEVAL_BENCHMARK)
    vector = build_vector_index(benchmark.index)
    calls = {"keyword": 0, "vector": 0}
    original_keyword = hybrid_module.search_keyword_index
    original_vector = hybrid_module.search_vector_index

    def keyword_spy(*args, **kwargs):
        calls["keyword"] += 1
        return original_keyword(*args, **kwargs)

    def vector_spy(*args, **kwargs):
        calls["vector"] += 1
        return original_vector(*args, **kwargs)

    monkeypatch.setattr(hybrid_module, "search_keyword_index", keyword_spy)
    monkeypatch.setattr(hybrid_module, "search_vector_index", vector_spy)
    response = search_hybrid_index(
        benchmark.index,
        vector,
        KeywordQuery(text="silver key observatory", limit=3),
    )

    assert calls == {"keyword": 1, "vector": 1}
    assert response.fusion_id == "rrf-v1"
    assert response.rrf_k == 60
    assert all(item.contributing_routes == ("keyword", "vector") for item in response.results)


def test_hybrid_rejects_indexes_from_different_corpora():
    benchmark = load_retrieval_benchmark(RETRIEVAL_BENCHMARK)
    vector = replace(build_vector_index(benchmark.index), source_index_id="other")

    try:
        search_hybrid_index(
            benchmark.index,
            vector,
            KeywordQuery(text="silver key", limit=1),
        )
    except ValueError as exc:
        assert "same document corpus" in str(exc)
    else:
        raise AssertionError("hybrid retrieval accepted mismatched indexes")
