import json
from dataclasses import replace
from pathlib import Path

from nagi.evaluation import (
    evaluate_qlie_translation_benchmark,
    load_qlie_translation_benchmark,
    write_qlie_translation_artifact,
)
from scripts import evaluate_qlie_translation


BENCHMARK_PATH = Path("benchmarks/qlie_translation_tasks.json")


def test_synthetic_translation_pipeline_reaches_guarded_dry_run(tmp_path):
    benchmark = load_qlie_translation_benchmark(BENCHMARK_PATH)
    workspace = tmp_path / "pipeline-workspace"

    report = evaluate_qlie_translation_benchmark(
        benchmark,
        workspace_root=workspace,
        retrieval_repeats=2,
    )
    payload = report.to_dict()

    assert report.passed is True
    assert report.failures == ()
    assert payload["metrics"] == {
        "applicable_terminology_count": 2,
        "candidate_count": 2,
        "dry_run_changed_file_count": 1,
        "dry_run_changed_unit_count": 2,
        "estimated_cost_usd": 0.0,
        "expected_token_count": 1,
        "matched_terminology_count": 2,
        "preserved_token_count": 1,
        "recovery_success_rate": 1.0,
        "retrieval_mrr": 1.0,
        "retrieval_recall_at_k": 1.0,
        "structure_retention": 1.0,
        "terminology_consistency": 1.0,
        "text_recall": 1.0,
    }
    assert [item["status"] for item in payload["stages"]] == ["passed"] * 5
    assert payload["stages"][2]["details"]["initial_model_calls"] == 2
    assert payload["stages"][2]["details"]["resume_model_calls"] == 1
    assert payload["stages"][2]["details"]["cache_hit_units"] == 1
    assert (workspace / "preview" / "manifest.json").is_file()
    assert not (workspace / "translated-sidecar").exists()
    assert payload["side_effects"] == {
        "network_called": False,
        "real_model_called": False,
        "game_modified": False,
        "pack_written": False,
        "translated_sidecar_published": False,
    }
    serialized = report.to_json()
    assert "Hello {name}" not in serialized
    assert "爱丽丝" not in serialized


def test_translation_benchmark_identity_and_metrics_are_reproducible(tmp_path):
    benchmark = load_qlie_translation_benchmark(BENCHMARK_PATH)
    first = evaluate_qlie_translation_benchmark(
        benchmark,
        workspace_root=tmp_path / "first",
        retrieval_repeats=1,
    )
    second = evaluate_qlie_translation_benchmark(
        benchmark,
        workspace_root=tmp_path / "second",
        retrieval_repeats=1,
    )

    assert first.artifact_id == second.artifact_id
    assert first.dataset == second.dataset
    assert first.config == second.config
    assert first.metrics == second.metrics
    assert first.failures == second.failures
    assert [(row["name"], row["status"]) for row in first.stages] == [
        (row["name"], row["status"]) for row in second.stages
    ]


def test_structurally_invalid_scripted_translation_fails_closed(tmp_path):
    benchmark = load_qlie_translation_benchmark(BENCHMARK_PATH)
    translations = dict(benchmark.translations_by_source_hash)
    translations[
        "d4351deb1ac25834c82e556a2c10bf52052df2819b35d7252bdbba39d8dc8231"
    ] = "「你好!」"
    invalid = replace(benchmark, translations_by_source_hash=translations)

    report = evaluate_qlie_translation_benchmark(
        invalid,
        workspace_root=tmp_path / "invalid",
        retrieval_repeats=1,
    )

    assert report.status == "failed"
    assert "quality_gate_failed" in report.failures
    assert "structure_retention_below_target" in report.failures
    assert "patch_dry_run_failed" in report.failures
    assert report.metrics["structure_retention"] == 0.0
    assert report.stages[-1]["status"] == "failed"
    assert not (tmp_path / "invalid" / "preview").exists()
    assert not (tmp_path / "invalid" / "translated-sidecar").exists()


def test_benchmark_script_writes_content_free_versioned_artifact(tmp_path, capsys):
    output = tmp_path / "qlie-translation-eval-v1.json"

    code = evaluate_qlie_translation.main(
        [str(BENCHMARK_PATH), "--retrieval-repeats", "1", "--output", str(output), "--json"]
    )
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    written = json.loads(output.read_text(encoding="utf-8"))

    assert code == 0
    assert payload == written
    assert payload["schema_version"] == 1
    assert payload["artifact_id"].startswith("qlie_translation_eval_v1_")
    assert payload["status"] == "passed"
    assert "Hello {name}" not in stdout
    assert "爱丽丝" not in stdout


def test_artifact_writer_rejects_non_report(tmp_path):
    try:
        write_qlie_translation_artifact({}, tmp_path / "artifact.json")
    except TypeError as exc:
        assert "report" in str(exc)
    else:
        raise AssertionError("non-report artifact payload must be rejected")
