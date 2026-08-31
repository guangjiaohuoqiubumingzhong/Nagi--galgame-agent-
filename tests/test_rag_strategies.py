import json
from pathlib import Path

import nagi.rag.strategies as strategy_module
from nagi.evaluation import (
    evaluate_qlie_retrieval_ablation,
    load_qlie_translation_benchmark,
)
from nagi.rag import (
    default_retrieval_strategy_specs,
    evaluate_retrieval_strategies,
    evaluate_retrieval_strategy,
    load_retrieval_benchmark,
)
from scripts import evaluate_qlie_rag_ablation


TRANSLATION_BENCHMARK = Path("benchmarks/qlie_translation_tasks.json")
RETRIEVAL_BENCHMARK = Path("benchmarks/qlie_retrieval_tasks.json")


def test_default_strategy_protocol_has_fixed_order_and_versioned_backends():
    specs = default_retrieval_strategy_specs()

    assert [item.name for item in specs] == ["no_rag", "keyword", "vector", "hybrid"]
    assert [item.available for item in specs] == [True, True, True, True]
    assert specs[0].backend == "disabled-control-v1"
    assert specs[1].backend == "bm25-v1"
    assert specs[2].backend == "feature-hashing-word-cjk-v1:d1024:l2"
    assert specs[2].unavailable_reason is None
    assert specs[3].backend == (
        "rrf-v1:k60:bm25-v1+feature-hashing-word-cjk-v1"
    )
    assert specs[3].unavailable_reason is None


def test_strategy_evaluation_keeps_control_unscored_and_vector_is_not_keyword_fallback(
    monkeypatch,
):
    benchmark = load_retrieval_benchmark(RETRIEVAL_BENCHMARK)
    specs = default_retrieval_strategy_specs()
    no_rag = evaluate_retrieval_strategy(specs[0], benchmark, repeats=1)

    assert no_rag.status == "completed"
    assert no_rag.metric_status == "not_applicable"
    assert no_rag.recall_at_k is None
    assert no_rag.mrr is None
    assert no_rag.latency_ms["p95"] == 0.0

    def fail_if_keyword_fallback(*_args, **_kwargs):
        raise AssertionError("vector strategy must not execute the keyword evaluator")

    monkeypatch.setattr(strategy_module, "evaluate_retrieval_benchmark", fail_if_keyword_fallback)
    vector = evaluate_retrieval_strategy(specs[2], benchmark, repeats=1)
    assert vector.status == "completed"
    assert vector.metric_status == "measured"
    assert vector.recall_at_k == 1.0
    assert vector.mrr == 1.0
    assert vector.index_id.startswith("vec_v1_")


def test_phase6c_measures_keyword_vector_and_hybrid_backends():
    benchmark = load_retrieval_benchmark(RETRIEVAL_BENCHMARK)
    results = evaluate_retrieval_strategies(benchmark, repeats=2)

    assert [item.status for item in results] == [
        "completed",
        "completed",
        "completed",
        "completed",
    ]
    assert [item.metric_status for item in results] == [
        "not_applicable",
        "measured",
        "measured",
        "measured",
    ]
    for result in results[1:]:
        assert result.recall_at_k == 1.0
        assert result.mrr == 1.0
        assert result.latency_ms is not None


def test_phase6c_ablation_shares_tasks_candidates_and_constraints(tmp_path):
    benchmark = load_qlie_translation_benchmark(TRANSLATION_BENCHMARK)
    report = evaluate_qlie_retrieval_ablation(
        benchmark,
        workspace_root=tmp_path / "ablation",
        retrieval_repeats=1,
    )
    payload = report.to_dict()

    assert report.passed is True
    assert report.comparison_status == "complete"
    assert report.failures == ()
    assert payload["comparison_contract"]["same_task_set"] is True
    assert payload["comparison_contract"]["same_translation_candidates"] is True
    assert payload["comparison_contract"]["same_translation_constraints"] is True
    assert payload["summary"] == {
        "strategy_count": 4,
        "completed_count": 4,
        "unavailable_count": 0,
        "measured_count": 3,
        "intentionally_unscored_count": 1,
        "comparison_ready": True,
    }
    rows = {item["strategy"]["name"]: item for item in payload["strategies"]}
    assert rows["no_rag"]["recall_at_k"] is None
    assert rows["keyword"]["recall_at_k"] == 1.0
    assert rows["vector"]["recall_at_k"] == 1.0
    assert rows["hybrid"]["recall_at_k"] == 1.0
    assert rows["vector"]["index_id"].startswith("vec_v1_")
    assert rows["hybrid"]["index_id"].startswith("hybrid_v1_")
    assert payload["config"]["unavailable_metric_policy"] == "null_not_zero"
    assert payload["config"]["vector"]["dimensions"] == 1024
    assert payload["config"]["vector"]["normalization"] == "l2"
    assert payload["config"]["hybrid"]["routes"] == ["keyword", "vector"]
    assert payload["config"]["hybrid"]["rrf_k"] == 60
    assert payload["shared_translation_metrics"]["structure_retention"] == 1.0
    assert payload["shared_translation_metrics"]["terminology_consistency"] == 1.0


def test_ablation_identity_is_stable_and_cli_artifact_is_content_free(tmp_path, capsys):
    benchmark = load_qlie_translation_benchmark(TRANSLATION_BENCHMARK)
    first = evaluate_qlie_retrieval_ablation(
        benchmark,
        workspace_root=tmp_path / "first",
        retrieval_repeats=1,
    )
    second = evaluate_qlie_retrieval_ablation(
        benchmark,
        workspace_root=tmp_path / "second",
        retrieval_repeats=1,
    )
    assert first.artifact_id == second.artifact_id
    assert first.comparison_contract == second.comparison_contract
    assert first.shared_translation_metrics == second.shared_translation_metrics

    output = tmp_path / "qlie-rag-ablation-v1.json"
    code = evaluate_qlie_rag_ablation.main(
        [
            str(TRANSLATION_BENCHMARK),
            "--retrieval-repeats",
            "1",
            "--output",
            str(output),
            "--json",
        ]
    )
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert code == 0
    assert payload == json.loads(output.read_text(encoding="utf-8"))
    assert payload["artifact_id"].startswith("qlie_rag_ablation_v1_")
    assert "Hello {name}" not in stdout
    assert "爱丽丝" not in stdout
    for row in payload["strategies"]:
        if row["status"] == "unavailable":
            assert row["recall_at_k"] is None
            assert row["mrr"] is None
            assert row["latency_ms"] is None
