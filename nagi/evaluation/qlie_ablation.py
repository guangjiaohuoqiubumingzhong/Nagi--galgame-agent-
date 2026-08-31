"""Phase 6C retrieval ablation over the fixed synthetic QLIE benchmark."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..rag import (
    HYBRID_BACKEND_ID,
    RRF_K,
    VECTOR_BACKEND_ID,
    VECTOR_DIMENSIONS,
    VECTOR_MODEL_ID,
    VECTOR_NORMALIZATION,
    evaluate_retrieval_strategies,
    load_retrieval_benchmark,
)
from .qlie_translation import (
    QlieTranslationBenchmark,
    evaluate_qlie_translation_benchmark,
)


QLIE_RETRIEVAL_ABLATION_SCHEMA_VERSION = 1


def _sha256(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(payload):
    return _sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


@dataclass(frozen=True)
class QlieRetrievalAblationReport:
    artifact_id: str
    status: str
    comparison_status: str
    dataset: dict
    config: dict
    comparison_contract: dict
    shared_translation_metrics: dict
    strategies: tuple[dict, ...]
    summary: dict
    failures: tuple[str, ...]
    limitations: tuple[str, ...]
    environment: dict

    @property
    def passed(self):
        return self.status == "passed"

    def to_dict(self):
        return {
            "schema_version": QLIE_RETRIEVAL_ABLATION_SCHEMA_VERSION,
            "artifact_id": self.artifact_id,
            "status": self.status,
            "comparison_status": self.comparison_status,
            "dataset": dict(self.dataset),
            "config": dict(self.config),
            "comparison_contract": dict(self.comparison_contract),
            "shared_translation_metrics": dict(self.shared_translation_metrics),
            "strategies": [dict(item) for item in self.strategies],
            "summary": dict(self.summary),
            "failures": list(self.failures),
            "limitations": list(self.limitations),
            "environment": dict(self.environment),
            "side_effects": {
                "network_called": False,
                "real_model_called": False,
                "game_modified": False,
                "pack_written": False,
                "translated_sidecar_published": False,
            },
        }

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _translation_candidate_set_sha256(benchmark):
    return _canonical_sha256(
        [
            {
                "source_text_sha256": source_hash,
                "translated_text_sha256": _sha256(translated_text),
            }
            for source_hash, translated_text in sorted(
                benchmark.translations_by_source_hash.items()
            )
        ]
    )


def evaluate_qlie_retrieval_ablation(
    benchmark,
    *,
    workspace_root=None,
    retrieval_repeats=None,
):
    if not isinstance(benchmark, QlieTranslationBenchmark):
        raise TypeError("benchmark must be a QlieTranslationBenchmark")
    repeats = benchmark.retrieval_repeats if retrieval_repeats is None else retrieval_repeats
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 1000:
        raise ValueError("retrieval_repeats must be between 1 and 1000")

    baseline = evaluate_qlie_translation_benchmark(
        benchmark,
        workspace_root=workspace_root,
        retrieval_repeats=repeats,
    )
    retrieval_benchmark = load_retrieval_benchmark(benchmark.retrieval_path)
    strategy_results = evaluate_retrieval_strategies(
        retrieval_benchmark,
        repeats=repeats,
    )
    rows = tuple(result.to_dict() for result in strategy_results)

    failures = []
    if not baseline.passed:
        failures.append("shared_translation_baseline_failed")
    measured = {
        item.strategy.name: item
        for item in strategy_results
        if item.strategy.name != "no_rag"
    }
    for name in ("keyword", "vector", "hybrid"):
        result = measured[name]
        if result.status != "completed" or result.metric_status != "measured":
            failures.append(f"{name}_measurement_contract_failed")
        elif result.recall_at_k != 1.0:
            failures.append(f"{name}_recall_below_target")
    no_rag = next(item for item in strategy_results if item.strategy.name == "no_rag")
    if no_rag.status != "completed" or no_rag.metric_status != "not_applicable":
        failures.append("no_rag_control_contract_failed")
    unavailable = tuple(item for item in strategy_results if item.status == "unavailable")
    if unavailable:
        failures.append("retrieval_backend_unavailable")

    candidate_set_sha256 = _translation_candidate_set_sha256(benchmark)
    constraints_sha256 = _canonical_sha256(
        {
            "model": benchmark.translation["model_id"],
            "prompt_version": benchmark.translation["prompt_version"],
            "terminology_snapshot_sha256": benchmark.terminology_snapshot.snapshot_sha256,
            "target_language": benchmark.translation["target_language"],
        }
    )
    shared_metrics = {
        name: baseline.metrics[name]
        for name in (
            "text_recall",
            "structure_retention",
            "terminology_consistency",
            "recovery_success_rate",
            "candidate_count",
            "estimated_cost_usd",
        )
    }
    summary = {
        "strategy_count": len(strategy_results),
        "completed_count": sum(item.status == "completed" for item in strategy_results),
        "unavailable_count": len(unavailable),
        "measured_count": sum(item.metric_status == "measured" for item in strategy_results),
        "intentionally_unscored_count": sum(
            item.metric_status == "not_applicable" for item in strategy_results
        ),
        "comparison_ready": not unavailable,
    }
    config = {
        "strategy_protocol_version": 1,
        "retrieval_repeats": repeats,
        "translation_baseline_artifact_id": baseline.artifact_id,
        "unavailable_metric_policy": "null_not_zero",
        "vector": {
            "backend": VECTOR_BACKEND_ID,
            "model_id": VECTOR_MODEL_ID,
            "dimensions": VECTOR_DIMENSIONS,
            "normalization": VECTOR_NORMALIZATION,
        },
        "hybrid": {
            "backend": HYBRID_BACKEND_ID,
            "fusion": "reciprocal_rank_fusion",
            "rrf_k": RRF_K,
            "routes": ["keyword", "vector"],
        },
    }
    comparison_contract = {
        "same_task_set": True,
        "same_translation_candidates": True,
        "same_translation_constraints": True,
        "task_set_sha256": benchmark.retrieval_sha256,
        "translation_candidate_set_sha256": candidate_set_sha256,
        "translation_constraints_sha256": constraints_sha256,
    }
    stable_identity = {
        "schema_version": QLIE_RETRIEVAL_ABLATION_SCHEMA_VERSION,
        "dataset_sha256": benchmark.dataset_sha256,
        "config": config,
        "comparison_contract": comparison_contract,
        "shared_translation_metrics": shared_metrics,
        "strategies": [item.stable_dict() for item in strategy_results],
        "summary": summary,
        "failures": failures,
    }
    return QlieRetrievalAblationReport(
        artifact_id="qlie_rag_ablation_v1_" + _canonical_sha256(stable_identity),
        status="passed" if not failures else "failed",
        comparison_status=("complete" if not unavailable else "partial_backend_coverage"),
        dataset={
            "dataset_id": benchmark.dataset_id,
            "dataset_sha256": benchmark.dataset_sha256,
            "retrieval_dataset_sha256": benchmark.retrieval_sha256,
            "provenance": "original synthetic fixtures; no commercial game text",
        },
        config=config,
        comparison_contract=comparison_contract,
        shared_translation_metrics=shared_metrics,
        strategies=rows,
        summary=summary,
        failures=tuple(failures),
        limitations=(
            "offline feature hashing measures reproducible lexical vector retrieval, not neural semantic understanding",
            "no_rag is an intentionally unscored control, not a zero-recall system",
            "scripted translations isolate retrieval plumbing and do not measure human translation quality",
        ),
        environment=dict(baseline.environment),
    )


def write_qlie_retrieval_ablation_artifact(report, output_path):
    if not isinstance(report, QlieRetrievalAblationReport):
        raise TypeError("report must be a QlieRetrievalAblationReport")
    output = Path(output_path).expanduser().absolute()
    for current in (output, *output.parents):
        if current.exists() and current.is_symlink():
            raise ValueError("artifact output must not traverse symbolic links")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(output.parent),
        prefix=output.name + ".",
        suffix=".tmp",
    ) as stream:
        stream.write(report.to_json())
        temporary = Path(stream.name)
    temporary.replace(output)
    return output.resolve()


def render_qlie_retrieval_ablation_text(report):
    if not isinstance(report, QlieRetrievalAblationReport):
        raise TypeError("report must be a QlieRetrievalAblationReport")
    rows = {item["strategy"]["name"]: item for item in report.strategies}
    def measured_line(name):
        row = rows[name]
        return (
            f"{name}: completed, Recall@K={row['recall_at_k']:.6f}, "
            f"MRR={row['mrr']:.6f}"
        )

    return "\n".join(
        (
            f"QLIE RAG ablation: {report.status}",
            f"comparison_status: {report.comparison_status}",
            f"artifact_id: {report.artifact_id}",
            "no_rag: completed, retrieval metrics not applicable",
            measured_line("keyword"),
            measured_line("vector"),
            measured_line("hybrid"),
            f"failures: {', '.join(report.failures) if report.failures else 'none'}",
        )
    )


__all__ = [
    "QLIE_RETRIEVAL_ABLATION_SCHEMA_VERSION",
    "QlieRetrievalAblationReport",
    "evaluate_qlie_retrieval_ablation",
    "render_qlie_retrieval_ablation_text",
    "write_qlie_retrieval_ablation_artifact",
]
