"""Fixed, content-safe evaluation for Nagi's deterministic retrieval baseline."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .index import LoadedKeywordIndex
from .ingest import tokenize_keyword_text
from .models import KeywordDocument, KeywordQuery, RAG_SCHEMA_VERSION
from .retriever import search_keyword_index


BENCHMARK_SCHEMA_VERSION = 1
MAX_BENCHMARK_BYTES = 4 * 1024 * 1024
MAX_BENCHMARK_DOCUMENTS = 10_000
MAX_BENCHMARK_TASKS = 1_000
FILTER_FIELDS = {
    "kinds",
    "speaker",
    "scene",
    "archive_name",
    "output_path_prefix",
    "translatable",
}


@dataclass(frozen=True)
class RetrievalTask:
    task_id: str
    query: str
    relevant_segment_ids: tuple[str, ...]
    k: int
    filters: dict


@dataclass(frozen=True)
class RetrievalBenchmark:
    dataset_id: str
    dataset_sha256: str
    index: LoadedKeywordIndex
    tasks: tuple[RetrievalTask, ...]


@dataclass(frozen=True)
class RetrievalEvaluationReport:
    dataset_id: str
    dataset_sha256: str
    index_id: str
    repeats: int
    task_rows: tuple[dict, ...]
    recall_at_k: float
    mrr: float
    latency_ms: dict

    @property
    def passed(self):
        return all(row["recall_at_k"] == 1.0 for row in self.task_rows)

    def to_dict(self):
        return {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "status": "passed" if self.passed else "failed",
            "dataset_id": self.dataset_id,
            "dataset_sha256": self.dataset_sha256,
            "index_id": self.index_id,
            "repeats": self.repeats,
            "summary": {
                "task_count": len(self.task_rows),
                "recall_at_k": round(self.recall_at_k, 6),
                "mrr": round(self.mrr, 6),
                "latency_ms": self.latency_ms,
            },
            "tasks": list(self.task_rows),
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def _require_exact_keys(payload, expected, label):
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError(f"{label} fields do not match benchmark schema version 1")


def _nonempty(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _percentile(values, percentile):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[rank]


def _build_synthetic_index(payload, dataset_sha256, source_path):
    documents_payload = payload["documents"]
    if not isinstance(documents_payload, list) or not 1 <= len(documents_payload) <= MAX_BENCHMARK_DOCUMENTS:
        raise ValueError("benchmark documents must be a non-empty bounded list")
    documents = []
    postings = defaultdict(list)
    segment_ids = set()
    expected_fields = {
        "segment_id",
        "text",
        "kind",
        "speaker",
        "scene",
        "archive_name",
        "output_path",
        "translatable",
    }
    for doc_id, item in enumerate(documents_payload):
        _require_exact_keys(item, expected_fields, f"benchmark document {doc_id}")
        segment_id = _nonempty(item["segment_id"], "document segment_id")
        text = _nonempty(item["text"], "document text")
        if segment_id in segment_ids:
            raise ValueError("benchmark document segment IDs must be unique")
        segment_ids.add(segment_id)
        if not isinstance(item["translatable"], bool):
            raise ValueError("document translatable must be a boolean")
        for field in ("speaker", "scene"):
            if item[field] is not None and not isinstance(item[field], str):
                raise ValueError(f"document {field} must be a string or null")
        tokens = tokenize_keyword_text(text)
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        document = KeywordDocument(
            doc_id=doc_id,
            segment_id=segment_id,
            kind=_nonempty(item["kind"], "document kind"),
            translatable=item["translatable"],
            normalized_text=text,
            speaker=item["speaker"],
            scene=item["scene"],
            archive_name=_nonempty(item["archive_name"], "document archive_name"),
            internal_path=_nonempty(item["output_path"], "document output_path"),
            output_path=item["output_path"],
            source_sha256=source_hash,
            byte_start=0,
            byte_end=len(text.encode("utf-8")),
            token_count=len(tokens),
        )
        documents.append(document)
        for token, frequency in sorted(Counter(tokens).items()):
            postings[token].append((doc_id, frequency))
    average_length = sum(document.token_count for document in documents) / len(documents)
    index_id = "benchmark_kw_v1_" + hashlib.sha256(
        f"{payload['dataset_id']}:{dataset_sha256}".encode("utf-8")
    ).hexdigest()
    return LoadedKeywordIndex(
        index_id=index_id,
        scope="all",
        source_segments_path=str(source_path),
        source_segments_sha256=dataset_sha256,
        documents=tuple(documents),
        postings={token: tuple(values) for token, values in sorted(postings.items())},
        average_document_length=average_length,
        manifest={"benchmark": True, "dataset_id": payload["dataset_id"]},
    )


def _load_tasks(payload, known_segment_ids):
    task_payloads = payload["tasks"]
    if not isinstance(task_payloads, list) or not 1 <= len(task_payloads) <= MAX_BENCHMARK_TASKS:
        raise ValueError("benchmark tasks must be a non-empty bounded list")
    tasks = []
    task_ids = set()
    expected_fields = {"id", "query", "relevant_segment_ids", "k", "filters"}
    for index, item in enumerate(task_payloads):
        _require_exact_keys(item, expected_fields, f"benchmark task {index}")
        task_id = _nonempty(item["id"], "task id")
        query = _nonempty(item["query"], "task query")
        if task_id in task_ids:
            raise ValueError("benchmark task IDs must be unique")
        task_ids.add(task_id)
        relevant = item["relevant_segment_ids"]
        if not isinstance(relevant, list) or not relevant or any(not isinstance(value, str) for value in relevant):
            raise ValueError("task relevant_segment_ids must be a non-empty string list")
        if len(set(relevant)) != len(relevant) or not set(relevant) <= known_segment_ids:
            raise ValueError("task relevant_segment_ids must be unique known document IDs")
        k = item["k"]
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
            raise ValueError("task k must be an integer between 1 and 100")
        filters = item["filters"]
        if not isinstance(filters, dict) or not set(filters) <= FILTER_FIELDS:
            raise ValueError("task filters contain unsupported fields")
        filters = dict(filters)
        if "kinds" in filters:
            if (
                not isinstance(filters["kinds"], list)
                or any(not isinstance(value, str) or not value for value in filters["kinds"])
            ):
                raise ValueError("task kinds filter must be a string list")
            filters["kinds"] = tuple(filters["kinds"])
        for field in ("speaker", "scene", "archive_name", "output_path_prefix"):
            if field in filters and filters[field] is not None and not isinstance(filters[field], str):
                raise ValueError(f"task {field} filter must be a string or null")
        if (
            "translatable" in filters
            and filters["translatable"] is not None
            and not isinstance(filters["translatable"], bool)
        ):
            raise ValueError("task translatable filter must be a boolean or null")
        tasks.append(
            RetrievalTask(
                task_id=task_id,
                query=query,
                relevant_segment_ids=tuple(relevant),
                k=k,
                filters=filters,
            )
        )
    return tuple(tasks)


def load_retrieval_benchmark(path):
    path = Path(path).expanduser().resolve()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BENCHMARK_BYTES:
        raise ValueError("retrieval benchmark must be a small regular file")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"retrieval benchmark JSON is invalid: {exc}") from exc
    _require_exact_keys(
        payload,
        {"schema_version", "dataset_id", "description", "documents", "tasks"},
        "retrieval benchmark",
    )
    if payload["schema_version"] != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported retrieval benchmark schema version")
    _nonempty(payload["dataset_id"], "dataset_id")
    _nonempty(payload["description"], "description")
    dataset_sha256 = hashlib.sha256(raw).hexdigest()
    index = _build_synthetic_index(payload, dataset_sha256, path)
    tasks = _load_tasks(payload, {document.segment_id for document in index.documents})
    return RetrievalBenchmark(
        dataset_id=payload["dataset_id"],
        dataset_sha256=dataset_sha256,
        index=index,
        tasks=tasks,
    )


def evaluate_retrieval_benchmark(benchmark, *, repeats=20):
    if not isinstance(benchmark, RetrievalBenchmark):
        raise TypeError("benchmark must be a RetrievalBenchmark")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 1_000:
        raise ValueError("repeats must be an integer between 1 and 1000")
    rows = []
    all_latencies = []
    for task in benchmark.tasks:
        query = KeywordQuery(text=task.query, limit=task.k, **task.filters)
        response = search_keyword_index(benchmark.index, query)
        retrieved = [result.document.segment_id for result in response.results]
        relevant = set(task.relevant_segment_ids)
        recall = len(relevant.intersection(retrieved[: task.k])) / len(relevant)
        first_rank = next(
            (rank for rank, segment_id in enumerate(retrieved, start=1) if segment_id in relevant),
            None,
        )
        reciprocal_rank = 0.0 if first_rank is None else 1.0 / first_rank
        task_latencies = []
        for _ in range(repeats):
            started = time.perf_counter()
            repeated = search_keyword_index(benchmark.index, query)
            task_latencies.append((time.perf_counter() - started) * 1000)
            if [result.document.segment_id for result in repeated.results] != retrieved:
                raise RuntimeError("retrieval results changed during one benchmark run")
        all_latencies.extend(task_latencies)
        rows.append(
            {
                "id": task.task_id,
                "k": task.k,
                "relevant_count": len(relevant),
                "retrieved_segment_ids": retrieved,
                "recall_at_k": round(recall, 6),
                "reciprocal_rank": round(reciprocal_rank, 6),
                "median_latency_ms": round(statistics.median(task_latencies), 6),
            }
        )
    return RetrievalEvaluationReport(
        dataset_id=benchmark.dataset_id,
        dataset_sha256=benchmark.dataset_sha256,
        index_id=benchmark.index.index_id,
        repeats=repeats,
        task_rows=tuple(rows),
        recall_at_k=statistics.fmean(row["recall_at_k"] for row in rows),
        mrr=statistics.fmean(row["reciprocal_rank"] for row in rows),
        latency_ms={
            "mean": round(statistics.fmean(all_latencies), 6),
            "p50": round(statistics.median(all_latencies), 6),
            "p95": round(_percentile(all_latencies, 0.95), 6),
            "max": round(max(all_latencies), 6),
        },
    )


def render_retrieval_evaluation_text(report):
    status = "passed" if report.passed else "failed"
    return "\n".join(
        [
            f"Retrieval benchmark: {status}",
            f"dataset: {report.dataset_id}",
            f"tasks: {len(report.task_rows)}; repeats: {report.repeats}",
            f"Recall@K: {report.recall_at_k:.6f}; MRR: {report.mrr:.6f}",
            (
                f"latency_ms: p50={report.latency_ms['p50']:.6f}; "
                f"p95={report.latency_ms['p95']:.6f}; max={report.latency_ms['max']:.6f}"
            ),
        ]
    )
