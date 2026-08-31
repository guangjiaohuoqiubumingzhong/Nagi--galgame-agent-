import hashlib
import json
from pathlib import Path

import pytest

from nagi import cli
from nagi.gameio.segments import SegmentSource, build_text_segment, segments_to_jsonl
from nagi.rag.index import (
    apply_keyword_index_plan,
    build_keyword_index_plan,
    load_keyword_index,
)
from nagi.rag.context import KeywordContextRetriever
from nagi.rag.evaluation import evaluate_retrieval_benchmark, load_retrieval_benchmark
from nagi.rag.ingest import tokenize_keyword_text
from nagi.rag.models import KeywordQuery
from nagi.rag.retriever import search_keyword_index


def make_segment(index, text, *, kind, translatable, speaker=None, scene=None, output_path=None):
    data = text.encode("utf-8")
    path = output_path or f"resolved/scenario/{index}.s"
    source = SegmentSource(
        engine="qlie",
        archive_name="GameData/data8.pack",
        internal_path=f"scenario\\{index}.s",
        output_path=path,
        entry_index=index,
        conflict_group=None,
        source_sha256=hashlib.sha256(data).hexdigest(),
        source_size_bytes=len(data),
        encoding="utf-8",
        byte_start=0,
        byte_end=len(data),
        line_start=1,
        line_end=1,
    )
    return build_text_segment(
        kind=kind,
        source=source,
        source_text=text,
        translatable=translatable,
        speaker=speaker,
        scene=scene,
    )


def write_corpus(root):
    root.mkdir()
    segments = (
        make_segment(1, "月の光が綺麗", kind="dialogue", translatable=True, speaker="Alice", scene="intro"),
        make_segment(2, "月は静か", kind="dialogue", translatable=True, speaker="Bob", scene="night"),
        make_segment(3, "庭に白い花", kind="narration", translatable=True, scene="intro"),
        make_segment(4, "jmp end", kind="control", translatable=False, output_path="resolved/system/control.s"),
    )
    text = segments_to_jsonl(segments)
    segments_path = root / "segments.jsonl"
    with segments_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    source_hash = hashlib.sha256(segments_path.read_bytes()).hexdigest()
    report = {
        "artifacts": {
            "segments": {
                "path": "segments.jsonl",
                "sha256": source_hash,
            }
        }
    }
    (root / "parse-report.json").write_text(json.dumps(report), encoding="utf-8")
    return segments, source_hash


def test_keyword_tokenizer_is_stable_for_cjk_and_words():
    assert tokenize_keyword_text("Moon 月光 MOON") == (
        "w:moon",
        "c:月",
        "c:光",
        "b:月光",
        "w:moon",
    )


def test_index_plan_is_content_free_and_dry_run(tmp_path):
    corpus = tmp_path / "corpus"
    output = tmp_path / "index"
    write_corpus(corpus)

    plan = build_keyword_index_plan(corpus, output)
    payload = plan.to_dict()

    assert plan.status == "ready"
    assert payload["summary"]["source_segment_count"] == 4
    assert payload["summary"]["indexed_document_count"] == 3
    assert payload["scope"] == "translatable"
    assert "月の光が綺麗" not in plan.to_json()
    assert not output.exists()


def test_published_index_searches_and_filters_with_bm25(tmp_path):
    corpus = tmp_path / "corpus"
    output = tmp_path / "index"
    segments, _ = write_corpus(corpus)
    plan = build_keyword_index_plan(corpus, output)
    result = apply_keyword_index_plan(plan)
    index = load_keyword_index(output)

    response = search_keyword_index(index, KeywordQuery(text="月 光", limit=5))
    alice_only = search_keyword_index(
        index,
        KeywordQuery(text="月", speaker="alice", scene="INTRO", limit=5),
    )
    narration_only = search_keyword_index(
        index,
        KeywordQuery(text="花", kinds=("narration",), output_path_prefix="resolved/scenario"),
    )

    assert result.status == "published"
    assert response.results[0].document.segment_id == segments[0].segment_id
    assert {item.document.speaker for item in response.results} == {"Alice", "Bob"}
    assert [item.document.speaker for item in alice_only.results] == ["Alice"]
    assert [item.document.kind for item in narration_only.results] == ["narration"]
    assert all(item.score > 0 for item in response.results)
    assert all(item.matched_tokens for item in response.results)

    context_candidates = KeywordContextRetriever(
        index,
        filters={"speaker": "alice"},
    )("月", limit=2)
    assert [item["segment_id"] for item in context_candidates] == [segments[0].segment_id]
    assert context_candidates[0]["text"] == "月の光が綺麗"
    assert context_candidates[0]["index_id"] == index.index_id
    assert context_candidates[0]["source"].endswith(":0-18")


def test_all_scope_supports_nontranslatable_filter(tmp_path):
    corpus = tmp_path / "corpus"
    output = tmp_path / "index"
    segments, _ = write_corpus(corpus)
    plan = build_keyword_index_plan(corpus, output, scope="all")
    apply_keyword_index_plan(plan)
    index = load_keyword_index(output)

    response = search_keyword_index(
        index,
        KeywordQuery(text="jmp", translatable=False),
    )

    assert [item.document.segment_id for item in response.results] == [segments[3].segment_id]


def test_index_artifacts_are_deterministic_across_output_locations(tmp_path):
    corpus = tmp_path / "corpus"
    output_one = tmp_path / "index-one"
    output_two = tmp_path / "index-two"
    write_corpus(corpus)

    apply_keyword_index_plan(build_keyword_index_plan(corpus, output_one))
    apply_keyword_index_plan(build_keyword_index_plan(corpus, output_two))

    assert (output_one / "documents.jsonl").read_bytes() == (
        output_two / "documents.jsonl"
    ).read_bytes()
    assert (output_one / "postings.jsonl").read_bytes() == (
        output_two / "postings.jsonl"
    ).read_bytes()
    first_manifest = json.loads((output_one / "manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((output_two / "manifest.json").read_text(encoding="utf-8"))
    assert first_manifest == second_manifest


def test_source_change_invalidates_plan_and_loaded_index(tmp_path):
    corpus = tmp_path / "corpus"
    output = tmp_path / "index"
    write_corpus(corpus)
    plan = build_keyword_index_plan(corpus, output)
    (corpus / "segments.jsonl").write_bytes(
        (corpus / "segments.jsonl").read_bytes() + b"\n"
    )

    result = apply_keyword_index_plan(plan)

    assert result.status == "source_changed"
    assert not output.exists()

    clean_corpus = tmp_path / "clean-corpus"
    clean_output = tmp_path / "clean-index"
    write_corpus(clean_corpus)
    apply_keyword_index_plan(build_keyword_index_plan(clean_corpus, clean_output))
    (clean_corpus / "segments.jsonl").write_bytes(
        (clean_corpus / "segments.jsonl").read_bytes() + b"\n"
    )
    with pytest.raises(ValueError, match="stale"):
        load_keyword_index(clean_output)


def test_publish_failure_cleans_staging_and_git_output_is_rejected(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    output = tmp_path / "index"
    write_corpus(corpus)
    plan = build_keyword_index_plan(corpus, output)

    def fail_write(_path, _values):
        raise OSError("synthetic index disk failure")

    monkeypatch.setattr("nagi.rag.index._write_jsonl", fail_write)
    result = apply_keyword_index_plan(plan)

    assert result.status == "write_failed"
    assert not output.exists()
    assert not any(path.name.startswith(".index.staging-") for path in tmp_path.iterdir())

    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    rejected = build_keyword_index_plan(corpus, repository / "index")
    assert rejected.status == "invalid_input"
    assert "Git worktree" in rejected.reason


def test_loaded_index_rejects_corrupt_artifacts(tmp_path):
    corpus = tmp_path / "corpus"
    output = tmp_path / "index"
    write_corpus(corpus)
    apply_keyword_index_plan(build_keyword_index_plan(corpus, output))
    (output / "postings.jsonl").write_bytes(
        (output / "postings.jsonl").read_bytes() + b"\n"
    )

    with pytest.raises(ValueError, match="hash"):
        load_keyword_index(output)


def test_rag_cli_does_not_build_agent_and_defaults_to_dry_run(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "corpus"
    output = tmp_path / "index"
    write_corpus(corpus)

    def fail_if_called(_args):
        raise AssertionError("RAG index commands must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)
    dry_code = cli.main(
        ["rag", "build-index", str(corpus), "--output", str(output), "--json"]
    )
    dry_output = capsys.readouterr().out
    dry_plan = json.loads(dry_output)
    apply_code = cli.main(
        [
            "rag",
            "build-index",
            str(corpus),
            "--output",
            str(output),
            "--apply",
            "--json",
        ]
    )
    capsys.readouterr()
    query_code = cli.main(
        [
            "rag",
            "query",
            str(output),
            "月",
            "--speaker",
            "Alice",
            "--metadata-only",
            "--json",
        ]
    )
    query_payload = json.loads(capsys.readouterr().out)

    assert dry_code == 0
    assert dry_plan["status"] == "ready"
    assert "月の光が綺麗" not in dry_output
    assert output.exists()
    assert apply_code == 0
    assert query_code == 0
    assert query_payload["summary"]["result_count"] == 1
    assert "normalized_text" not in query_payload["results"][0]["document"]


def test_fixed_synthetic_retrieval_benchmark_reports_recall_mrr_and_latency():
    benchmark_path = Path(__file__).parents[1] / "benchmarks" / "qlie_retrieval_tasks.json"
    benchmark = load_retrieval_benchmark(benchmark_path)

    report = evaluate_retrieval_benchmark(benchmark, repeats=3)
    payload = report.to_dict()

    assert report.passed is True
    assert payload["summary"]["task_count"] == 7
    assert payload["summary"]["recall_at_k"] == 1.0
    assert payload["summary"]["mrr"] == 1.0
    assert payload["summary"]["latency_ms"]["p50"] >= 0
    assert all(row["recall_at_k"] == 1.0 for row in payload["tasks"])
    assert "The silver key opens" not in report.to_json()


def test_rag_evaluate_cli_is_agent_independent(monkeypatch, capsys):
    benchmark_path = Path(__file__).parents[1] / "benchmarks" / "qlie_retrieval_tasks.json"

    def fail_if_called(_args):
        raise AssertionError("RAG evaluation must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)
    code = cli.main(["rag", "evaluate", str(benchmark_path), "--repeats", "2", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "passed"
    assert payload["summary"]["recall_at_k"] == 1.0
    assert payload["summary"]["mrr"] == 1.0
