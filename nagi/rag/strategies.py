"""Versioned retrieval-strategy contracts for comparable ablation runs."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass

from .evaluation import RetrievalBenchmark, evaluate_retrieval_benchmark
from .hybrid import HYBRID_FUSION_ID, RRF_K, hybrid_index_id, search_hybrid_index
from .models import KeywordQuery
from .vector import (
    VECTOR_DIMENSIONS,
    VECTOR_MODEL_ID,
    VECTOR_NORMALIZATION,
    build_vector_index,
    search_vector_index,
)


RAG_STRATEGY_PROTOCOL_VERSION = 1
RAG_STRATEGY_ORDER = ("no_rag", "keyword", "vector", "hybrid")
RAG_STRATEGY_STATUSES = frozenset({"completed", "unavailable"})
RAG_METRIC_STATUSES = frozenset({"measured", "not_applicable", "unavailable"})
VECTOR_BACKEND_ID = (
    f"{VECTOR_MODEL_ID}:d{VECTOR_DIMENSIONS}:{VECTOR_NORMALIZATION}"
)
HYBRID_BACKEND_ID = (
    f"{HYBRID_FUSION_ID}:k{RRF_K}:bm25-v1+{VECTOR_MODEL_ID}"
)


@dataclass(frozen=True)
class RetrievalStrategySpec:
    name: str
    backend: str | None
    available: bool
    unavailable_reason: str | None = None

    def __post_init__(self):
        if self.name not in RAG_STRATEGY_ORDER:
            raise ValueError("unsupported retrieval strategy name")
        if self.available:
            if not isinstance(self.backend, str) or not self.backend:
                raise ValueError("available retrieval strategies require a backend")
            if self.unavailable_reason is not None:
                raise ValueError("available retrieval strategies cannot have an unavailable reason")
        else:
            if self.backend is not None:
                raise ValueError("unavailable retrieval strategies cannot claim a backend")
            if not isinstance(self.unavailable_reason, str) or not self.unavailable_reason:
                raise ValueError("unavailable retrieval strategies require a reason")

    def to_dict(self):
        return {
            "protocol_version": RAG_STRATEGY_PROTOCOL_VERSION,
            "name": self.name,
            "backend": self.backend,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class RetrievalStrategyResult:
    strategy: RetrievalStrategySpec
    status: str
    metric_status: str
    task_count: int
    retrieved_result_count: int | None
    recall_at_k: float | None
    mrr: float | None
    latency_ms: dict | None
    index_id: str | None = None
    reason: str | None = None

    def __post_init__(self):
        if self.status not in RAG_STRATEGY_STATUSES:
            raise ValueError("unsupported retrieval strategy status")
        if self.metric_status not in RAG_METRIC_STATUSES:
            raise ValueError("unsupported retrieval metric status")
        if isinstance(self.task_count, bool) or not isinstance(self.task_count, int) or self.task_count < 1:
            raise ValueError("strategy task_count must be positive")
        if self.status == "unavailable":
            if self.strategy.available or self.metric_status != "unavailable":
                raise ValueError("unavailable result does not match its strategy contract")
            if any(
                value is not None
                for value in (
                    self.retrieved_result_count,
                    self.recall_at_k,
                    self.mrr,
                    self.latency_ms,
                    self.index_id,
                )
            ):
                raise ValueError("unavailable strategies must keep every metric null")
        if self.metric_status == "measured" and (
            self.status != "completed"
            or self.recall_at_k is None
            or self.mrr is None
            or self.latency_ms is None
        ):
            raise ValueError("measured strategy results require complete metrics")
        if self.metric_status == "not_applicable" and (
            self.status != "completed"
            or self.recall_at_k is not None
            or self.mrr is not None
        ):
            raise ValueError("not-applicable strategy metrics cannot contain retrieval scores")

    @property
    def scored(self):
        return self.metric_status == "measured"

    def stable_dict(self):
        return {
            "strategy": self.strategy.to_dict(),
            "status": self.status,
            "metric_status": self.metric_status,
            "task_count": self.task_count,
            "retrieved_result_count": self.retrieved_result_count,
            "recall_at_k": self.recall_at_k,
            "mrr": self.mrr,
            "index_id": self.index_id,
            "reason": self.reason,
        }

    def to_dict(self):
        payload = self.stable_dict()
        payload["scored"] = self.scored
        payload["latency_ms"] = None if self.latency_ms is None else dict(self.latency_ms)
        return payload


def default_retrieval_strategy_specs():
    return (
        RetrievalStrategySpec(name="no_rag", backend="disabled-control-v1", available=True),
        RetrievalStrategySpec(name="keyword", backend="bm25-v1", available=True),
        RetrievalStrategySpec(name="vector", backend=VECTOR_BACKEND_ID, available=True),
        RetrievalStrategySpec(name="hybrid", backend=HYBRID_BACKEND_ID, available=True),
    )


def _percentile(values, percentile):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[rank]


def _evaluate_search_backend(spec, benchmark, search, index_id, *, repeats, reason):
    recalls = []
    reciprocal_ranks = []
    latencies = []
    retrieved_result_count = 0
    for task in benchmark.tasks:
        query = KeywordQuery(text=task.query, limit=task.k, **task.filters)
        response = search(query)
        retrieved = [result.document.segment_id for result in response.results]
        retrieved_result_count += len(retrieved)
        relevant = set(task.relevant_segment_ids)
        recalls.append(len(relevant.intersection(retrieved[: task.k])) / len(relevant))
        first_rank = next(
            (
                rank
                for rank, segment_id in enumerate(retrieved, start=1)
                if segment_id in relevant
            ),
            None,
        )
        reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
        for _ in range(repeats):
            started = time.perf_counter()
            repeated = search(query)
            latencies.append((time.perf_counter() - started) * 1000)
            if [result.document.segment_id for result in repeated.results] != retrieved:
                raise RuntimeError("retrieval results changed during one strategy run")
    return RetrievalStrategyResult(
        strategy=spec,
        status="completed",
        metric_status="measured",
        task_count=len(benchmark.tasks),
        retrieved_result_count=retrieved_result_count,
        recall_at_k=round(statistics.fmean(recalls), 6),
        mrr=round(statistics.fmean(reciprocal_ranks), 6),
        latency_ms={
            "mean": round(statistics.fmean(latencies), 6),
            "p50": round(statistics.median(latencies), 6),
            "p95": round(_percentile(latencies, 0.95), 6),
            "max": round(max(latencies), 6),
        },
        index_id=index_id,
        reason=reason,
    )


def evaluate_retrieval_strategy(spec, benchmark, *, repeats=20):
    if not isinstance(spec, RetrievalStrategySpec):
        raise TypeError("spec must be a RetrievalStrategySpec")
    if not isinstance(benchmark, RetrievalBenchmark):
        raise TypeError("benchmark must be a RetrievalBenchmark")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 1000:
        raise ValueError("repeats must be an integer between 1 and 1000")
    task_count = len(benchmark.tasks)
    if not spec.available:
        return RetrievalStrategyResult(
            strategy=spec,
            status="unavailable",
            metric_status="unavailable",
            task_count=task_count,
            retrieved_result_count=None,
            recall_at_k=None,
            mrr=None,
            latency_ms=None,
            reason=spec.unavailable_reason,
        )
    if spec.name == "no_rag":
        return RetrievalStrategyResult(
            strategy=spec,
            status="completed",
            metric_status="not_applicable",
            task_count=task_count,
            retrieved_result_count=0,
            recall_at_k=None,
            mrr=None,
            latency_ms={"mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0},
            reason="control_intentionally_disables_retrieval",
        )
    if spec.name == "keyword":
        report = evaluate_retrieval_benchmark(benchmark, repeats=repeats)
        return RetrievalStrategyResult(
            strategy=spec,
            status="completed",
            metric_status="measured",
            task_count=len(report.task_rows),
            retrieved_result_count=sum(len(row["retrieved_segment_ids"]) for row in report.task_rows),
            recall_at_k=round(report.recall_at_k, 6),
            mrr=round(report.mrr, 6),
            latency_ms=dict(report.latency_ms),
            index_id=report.index_id,
            reason="deterministic_keyword_benchmark_completed",
        )
    vector_index = build_vector_index(benchmark.index)
    if spec.name == "vector":
        result = _evaluate_search_backend(
            spec,
            benchmark,
            lambda query: search_vector_index(vector_index, query),
            vector_index.index_id,
            repeats=repeats,
            reason="deterministic_offline_vector_benchmark_completed",
        )
    elif spec.name == "hybrid":
        result = _evaluate_search_backend(
            spec,
            benchmark,
            lambda query: search_hybrid_index(benchmark.index, vector_index, query),
            hybrid_index_id(benchmark.index, vector_index),
            repeats=repeats,
            reason="deterministic_dual_route_rrf_benchmark_completed",
        )
    else:
        raise RuntimeError(f"available backend for {spec.name} is not implemented")
    return result


def evaluate_retrieval_strategies(benchmark, *, specs=None, repeats=20):
    values = default_retrieval_strategy_specs() if specs is None else tuple(specs)
    if any(not isinstance(item, RetrievalStrategySpec) for item in values):
        raise TypeError("specs must contain RetrievalStrategySpec values")
    if tuple(item.name for item in values) != RAG_STRATEGY_ORDER:
        raise ValueError("strategy specs must contain no_rag, keyword, vector, hybrid in order")
    return tuple(
        evaluate_retrieval_strategy(spec, benchmark, repeats=repeats) for spec in values
    )


__all__ = [
    "RAG_METRIC_STATUSES",
    "RAG_STRATEGY_ORDER",
    "RAG_STRATEGY_PROTOCOL_VERSION",
    "RAG_STRATEGY_STATUSES",
    "HYBRID_BACKEND_ID",
    "VECTOR_BACKEND_ID",
    "RetrievalStrategyResult",
    "RetrievalStrategySpec",
    "default_retrieval_strategy_specs",
    "evaluate_retrieval_strategies",
    "evaluate_retrieval_strategy",
]
