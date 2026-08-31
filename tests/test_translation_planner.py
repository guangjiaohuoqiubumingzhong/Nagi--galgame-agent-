import hashlib
import json
import os
from unittest.mock import patch

import pytest

from nagi import Nagi, SessionStore, WorkspaceContext, cli
from nagi.gameio.segments import (
    SegmentInlineToken,
    SegmentSource,
    build_text_segment,
    segments_to_jsonl,
)
from nagi.providers.clients import FakeModelClient
from nagi.translation import (
    CharacterStyle,
    RAGEvidenceReference,
    RAGPromptEvidence,
    TranslationAdjacentContext,
    TranslationCacheError,
    TranslationRunBinding,
    TranslationTerm,
    build_translation_batch_plan,
    build_translation_candidate,
    build_translation_request,
    build_translation_run_spec,
    execute_translation_batch,
    execute_translation_run,
    initialize_translation_run,
    load_translation_checkpoint,
)


def make_segment(
    index,
    text,
    *,
    kind,
    translatable,
    speaker=None,
    scene=None,
    placeholders=(),
):
    data = text.encode("utf-8")
    source = SegmentSource(
        engine="synthetic",
        archive_name="Synthetic/data0.pack",
        internal_path=f"scenario\\{index}.s",
        output_path=f"synthetic/scenario/{index}.s",
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
        placeholders=placeholders,
    )


def write_published_corpus(root, *, review_status="accepted"):
    root.mkdir()
    segments = (
        make_segment(
            1,
            "Synthetic secret dialogue {hero}",
            kind="dialogue",
            translatable=True,
            speaker="Mira",
            scene="intro",
            placeholders=(
                SegmentInlineToken(kind="placeholder", raw="{hero}", char_start=26, char_end=32),
            ),
        ),
        make_segment(2, "Synthetic narration", kind="narration", translatable=True, scene="intro"),
        make_segment(3, "Synthetic choice", kind="choice", translatable=True, scene="route"),
        make_segment(4, "jump target", kind="control", translatable=False),
        make_segment(5, "reviewed unknown text", kind="unknown", translatable=False),
    )
    segments_path = root / "segments.jsonl"
    with segments_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(segments_to_jsonl(segments))
    source_hash = hashlib.sha256(segments_path.read_bytes()).hexdigest()
    report = {
        "schema_version": 1,
        "status": "published",
        "summary": {
            "segment_count": len(segments),
            "unknown_count": 1,
        },
        "unknown_review": {
            "status": review_status,
            "expected_count": 1,
            "reviewed_count": 1 if review_status == "accepted" else 0,
        },
        "artifacts": {
            "segments": {
                "path": "segments.jsonl",
                "sha256": source_hash,
            }
        },
    }
    with (root / "parse-report.json").open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return segments


def test_batch_plan_is_content_free_read_only_and_skips_unsafe_segments(tmp_path):
    corpus = tmp_path / "corpus"
    segments = write_published_corpus(corpus)
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in corpus.iterdir()
    }

    plan = build_translation_batch_plan(
        corpus,
        model_id="synthetic-model-v1",
        prompt_version="prompt-v2",
        terminology_version="terms-v3",
        rag_index_id="synthetic-index-v1",
        batch_size=2,
    )
    payload = plan.to_dict()
    serialized = plan.to_json()
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in corpus.iterdir()
    }

    assert plan.status == "ready"
    assert payload["summary"]["segment_count"] == 5
    assert payload["summary"]["selected_unit_count"] == 3
    assert payload["summary"]["batch_count"] == 2
    assert payload["summary"]["skipped_counts"] == {
        "nontranslatable": 1,
        "unknown": 1,
    }
    assert [batch["unit_count"] for batch in payload["batches"]] == [2, 1]
    assert all(batch["status"] == "planned" for batch in payload["batches"])
    assert payload["side_effects"] == {
        "model_called": False,
        "output_written": False,
        "game_modified": False,
    }
    assert segments[0].segment_id == payload["batches"][0]["units"][0]["segment_id"]
    assert payload["batches"][0]["units"][0]["source"]["source_text_sha256"] == segments[0].source_text_sha256
    assert "Synthetic secret dialogue" not in serialized
    assert "Synthetic narration" not in serialized
    assert "reviewed unknown text" not in serialized
    assert "source_text" not in payload["batches"][0]["units"][0]
    assert before == after
    assert sorted(path.name for path in corpus.iterdir()) == ["parse-report.json", "segments.jsonl"]


def test_plan_ids_and_cache_keys_are_deterministic_and_cover_versions(tmp_path):
    corpus = tmp_path / "corpus"
    write_published_corpus(corpus)
    options = {
        "model_id": "model-v1",
        "prompt_version": "prompt-v1",
        "terminology_version": "terms-v1",
        "rag_index_id": "index-v1",
        "batch_size": 2,
    }

    first = build_translation_batch_plan(corpus, **options)
    second = build_translation_batch_plan(corpus, **options)
    changed_model = build_translation_batch_plan(corpus, **{**options, "model_id": "model-v2"})
    changed_prompt = build_translation_batch_plan(corpus, **{**options, "prompt_version": "prompt-v2"})
    changed_terms = build_translation_batch_plan(corpus, **{**options, "terminology_version": "terms-v2"})
    changed_index = build_translation_batch_plan(corpus, **{**options, "rag_index_id": "index-v2"})

    first_keys = [unit.cache_key for batch in first.batches for unit in batch.units]
    assert first.to_json() == second.to_json()
    assert first.plan_id == second.plan_id
    assert first_keys == [unit.cache_key for batch in second.batches for unit in batch.units]
    for changed in (changed_model, changed_prompt, changed_terms, changed_index):
        assert changed.plan_id != first.plan_id
        assert [unit.cache_key for batch in changed.batches for unit in batch.units] != first_keys


def test_filters_and_limit_do_not_bypass_full_corpus_validation(tmp_path):
    corpus = tmp_path / "corpus"
    write_published_corpus(corpus)

    plan = build_translation_batch_plan(
        corpus,
        kinds=("dialogue", "narration"),
        scene="INTRO",
        output_path_prefix="synthetic/scenario",
        limit=1,
    )

    assert plan.status == "ready"
    assert plan.segment_count == 5
    assert plan.selected_unit_count == 1
    assert dict(plan.skipped_counts) == {
        "filtered": 1,
        "limit": 1,
        "nontranslatable": 1,
        "unknown": 1,
    }
    assert plan.batches[0].units[0].speaker == "Mira"


def test_missing_review_and_changed_corpus_are_rejected_without_outputs(tmp_path):
    unsafe = tmp_path / "unsafe"
    write_published_corpus(unsafe, review_status="required")

    unsafe_plan = build_translation_batch_plan(unsafe)
    assert unsafe_plan.status == "invalid_input"
    assert "review" in unsafe_plan.reason

    changed = tmp_path / "changed"
    write_published_corpus(changed)
    (changed / "segments.jsonl").write_bytes(
        (changed / "segments.jsonl").read_bytes() + b"\n"
    )
    changed_plan = build_translation_batch_plan(changed)
    assert changed_plan.status == "invalid_input"
    assert "record" in changed_plan.reason or "hash" in changed_plan.reason
    assert sorted(path.name for path in changed.iterdir()) == ["parse-report.json", "segments.jsonl"]


def test_candidate_contract_tracks_translation_hash_and_rag_evidence(tmp_path):
    corpus = tmp_path / "corpus"
    write_published_corpus(corpus)
    plan = build_translation_batch_plan(
        corpus,
        model_id="model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="index-v1",
    )
    unit = plan.batches[0].units[0]
    evidence = RAGEvidenceReference(
        index_id="index-v1",
        segment_id="synthetic:evidence",
        source="Synthetic/data0.pack:synthetic/scenario/evidence.s:0-10",
        score=3.25,
        content_sha256=hashlib.sha256(b"synthetic evidence").hexdigest(),
    )

    candidate = build_translation_candidate(
        unit,
        "合成译文",
        model_id="model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="index-v1",
        rag_evidence=(evidence,),
    )
    metadata_only = candidate.to_dict(include_text=False)

    assert candidate.segment_id == unit.segment_id
    assert candidate.cache_key != unit.cache_key
    assert candidate.translated_text_sha256 == hashlib.sha256("合成译文".encode("utf-8")).hexdigest()
    assert metadata_only["rag_evidence"] == [evidence.to_dict()]
    assert "translated_text" not in metadata_only
    with pytest.raises(ValueError, match="non-empty"):
        build_translation_candidate(
            unit,
            "",
            model_id="model-v1",
            prompt_version="prompt-v1",
            terminology_version="terms-v1",
            rag_index_id="index-v1",
        )


def test_translate_cli_is_dry_run_content_free_and_agent_independent(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "corpus"
    write_published_corpus(corpus)

    def fail_if_called(_args):
        raise AssertionError("translation planning must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)
    code = cli.main(
        [
            "translate",
            "plan-batch",
            str(corpus),
            "--model",
            "model-v1",
            "--batch-size",
            "2",
            "--json",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 0
    assert payload["status"] == "ready"
    assert payload["summary"]["selected_unit_count"] == 3
    assert payload["side_effects"]["model_called"] is False
    assert "Synthetic secret dialogue" not in output
    assert sorted(path.name for path in corpus.iterdir()) == ["parse-report.json", "segments.jsonl"]


def build_synthetic_translation_request(tmp_path):
    corpus = tmp_path / "corpus"
    write_published_corpus(corpus)
    plan = build_translation_batch_plan(
        corpus,
        model_id="fake-model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="index-v1",
        batch_size=2,
    )
    batch = plan.batches[0]
    first = batch.units[0]
    evidence_text = "Synthetic retrieved style example"
    evidence = RAGEvidenceReference(
        index_id="index-v1",
        segment_id="synthetic:evidence",
        source="Synthetic/data0.pack:synthetic/scenario/evidence.s:0-10",
        score=4.5,
        content_sha256=hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
    )
    request = build_translation_request(
        batch,
        model_id=plan.config.model_id,
        prompt_version=plan.config.prompt_version,
        terminology_version=plan.config.terminology_version,
        rag_index_id=plan.config.rag_index_id,
        target_language="zh-CN",
        terminology=(
            TranslationTerm("Synthetic term", "合成术语", "Keep this translation stable"),
        ),
        character_styles=(
            CharacterStyle("Mira", "Use a calm and restrained voice"),
        ),
        adjacent_context={
            first.unit_id: TranslationAdjacentContext(
                previous_text="Synthetic previous context",
                next_text="Synthetic next context",
            )
        },
        rag_context={
            first.unit_id: (RAGPromptEvidence(reference=evidence, text=evidence_text),)
        },
    )
    return corpus, request


def valid_response_payload(request):
    return {
        "schema_version": 1,
        "request_id": request.request_id,
        "translations": [
            {
                "unit_id": item.unit.unit_id,
                "segment_id": item.unit.segment_id,
                "translated_text": f"合成译文 {index}",
            }
            for index, item in enumerate(request.units, start=1)
        ],
    }


def test_request_is_deterministic_injects_constraints_and_keeps_trace_content_free(tmp_path):
    _corpus, request = build_synthetic_translation_request(tmp_path)
    metadata = request.to_json()

    assert request.request_id.startswith("trq_v1_")
    assert "Synthetic secret dialogue {hero}" in request.prompt
    assert "{hero}" in request.prompt
    assert "Synthetic previous context" in request.prompt
    assert "Synthetic next context" in request.prompt
    assert "Synthetic retrieved style example" in request.prompt
    assert "Synthetic term" in request.prompt
    assert "合成术语" in request.prompt
    assert "Use a calm and restrained voice" in request.prompt
    assert "Preserve every placeholder and tag raw value exactly" in request.prompt
    for secret_text in (
        "Synthetic secret dialogue",
        "Synthetic previous context",
        "Synthetic retrieved style example",
        "Synthetic term",
        "合成术语",
        "Use a calm and restrained voice",
    ):
        assert secret_text not in metadata
    assert request.to_dict()["side_effects"]["model_called"] is False


def test_fake_model_maps_strict_response_to_same_units_and_sanitizes_trace(tmp_path):
    _corpus, request = build_synthetic_translation_request(tmp_path)
    raw = json.dumps(valid_response_payload(request), ensure_ascii=False)
    model = FakeModelClient([raw])
    model.last_completion_metadata = {
        "input_tokens": 120,
        "output_tokens": 44,
        "api_key": "must-not-leak",
    }

    result = execute_translation_batch(request, model, max_new_tokens=512)
    trace = result.to_json()

    assert result.status == "completed"
    assert [item.unit_id for item in result.candidates] == [
        item.unit.unit_id for item in request.units
    ]
    assert [item.segment_id for item in result.candidates] == [
        item.unit.segment_id for item in request.units
    ]
    assert result.candidates[0].cache_key == request.units[0].cache_key
    assert model.message_requests == [request.messages]
    assert [item["role"] for item in request.messages] == ["system", "user"]
    assert "Synthetic secret dialogue" not in request.messages[0]["content"]
    assert "Synthetic retrieved style example" in request.messages[1]["content"]
    assert result.completion_metadata == {"input_tokens": 120, "output_tokens": 44}
    assert "must-not-leak" not in trace
    assert "合成译文" not in trace
    assert result.to_dict()["side_effects"] == {
        "model_called": True,
        "output_written": False,
        "game_modified": False,
    }


def test_malformed_or_misaligned_model_responses_reject_the_whole_batch(tmp_path):
    _corpus, request = build_synthetic_translation_request(tmp_path)
    valid = valid_response_payload(request)
    malformed = []
    malformed.append("```json\n{}\n```")

    wrong_request = json.loads(json.dumps(valid))
    wrong_request["request_id"] = "trq_v1_wrong"
    malformed.append(json.dumps(wrong_request))

    out_of_order = json.loads(json.dumps(valid))
    out_of_order["translations"].reverse()
    malformed.append(json.dumps(out_of_order))

    missing = json.loads(json.dumps(valid))
    missing["translations"].pop()
    malformed.append(json.dumps(missing))

    duplicate = json.loads(json.dumps(valid))
    duplicate["translations"][1] = dict(duplicate["translations"][0])
    malformed.append(json.dumps(duplicate))

    extra = json.loads(json.dumps(valid))
    extra["translations"].append(dict(extra["translations"][0]))
    malformed.append(json.dumps(extra))

    empty = json.loads(json.dumps(valid))
    empty["translations"][0]["translated_text"] = "  "
    malformed.append(json.dumps(empty))

    extra_key = json.loads(json.dumps(valid))
    extra_key["translations"][0]["confidence"] = 1
    malformed.append(json.dumps(extra_key))
    malformed.append(
        '{"schema_version":1,"schema_version":1,"request_id":"'
        + request.request_id
        + '","translations":[]}'
    )

    for raw in malformed:
        result = execute_translation_batch(request, FakeModelClient([raw]))
        assert result.status == "rejected"
        assert result.candidates == ()
        assert result.reason != "response_accepted"


def test_model_failure_is_content_free_and_produces_no_partial_candidates(tmp_path):
    _corpus, request = build_synthetic_translation_request(tmp_path)
    result = execute_translation_batch(request, FakeModelClient([]))

    assert result.status == "model_failed"
    assert result.reason == "model_call_failed"
    assert result.error_type == "RuntimeError"
    assert result.candidates == ()
    assert "fake model ran out" not in result.to_json()


def test_preview_request_cli_never_initializes_agent_or_calls_model(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "corpus"
    write_published_corpus(corpus)

    def fail_if_called(_args):
        raise AssertionError("request preview must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)
    code = cli.main(
        [
            "translate",
            "preview-request",
            str(corpus),
            "--model",
            "fake-model-v1",
            "--batch-size",
            "2",
            "--json",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 0
    assert payload["request_id"].startswith("trq_v1_")
    assert payload["unit_count"] == 2
    assert payload["side_effects"]["model_called"] is False
    assert "Synthetic secret dialogue" not in output
    assert "REQUEST_JSON" not in output
    assert sorted(path.name for path in corpus.iterdir()) == ["parse-report.json", "segments.jsonl"]


def build_synthetic_translation_run(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    corpus = tmp_path / "run-corpus"
    write_published_corpus(corpus)
    plan = build_translation_batch_plan(
        corpus,
        model_id="fake-model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="none",
        batch_size=2,
    )
    requests = tuple(
        build_translation_request(
            batch,
            model_id=plan.config.model_id,
            prompt_version=plan.config.prompt_version,
            terminology_version=plan.config.terminology_version,
            rag_index_id=plan.config.rag_index_id,
        )
        for batch in plan.batches
    )
    return corpus, plan, requests, build_translation_run_spec(plan, requests)


def test_translation_run_resumes_after_failure_without_repeating_completed_batch(tmp_path):
    corpus, plan, requests, spec = build_synthetic_translation_run(tmp_path)
    output = tmp_path / "translation-run"
    first_response = json.dumps(valid_response_payload(requests[0]), ensure_ascii=False)
    first_model = FakeModelClient([first_response])

    first = execute_translation_run(spec, output, first_model)
    first_checkpoint = load_translation_checkpoint(spec, output)

    assert first.status == "paused"
    assert first.reason == "model_call_failed"
    assert first.completed_batch_count == 1
    assert first.failed_batch_count == 1
    assert first.model_call_count == 2
    assert first.cache_hit_unit_count == 0
    assert [item["status"] for item in first_checkpoint["batches"]] == [
        "completed",
        "failed",
    ]
    assert [item["attempts"] for item in first_checkpoint["batches"]] == [1, 1]
    assert len(first_model.prompts) == 2

    second_response = json.dumps(valid_response_payload(requests[1]), ensure_ascii=False)
    second_model = FakeModelClient([second_response])
    resumed = execute_translation_run(spec, output, second_model, resume=True)
    final_checkpoint = load_translation_checkpoint(spec, output)

    assert resumed.status == "completed"
    assert resumed.reason == "all_batches_completed"
    assert resumed.completed_batch_count == 2
    assert resumed.failed_batch_count == 0
    assert resumed.pending_batch_count == 0
    assert resumed.completed_unit_count == plan.selected_unit_count == 3
    assert resumed.cache_hit_unit_count == 2
    assert resumed.model_call_count == 1
    assert resumed.attempt_count == 3
    assert resumed.retry_count == 1
    assert second_model.message_requests == [requests[1].messages]
    assert [item["status"] for item in final_checkpoint["batches"]] == [
        "completed",
        "completed",
    ]
    assert [item["attempts"] for item in final_checkpoint["batches"]] == [1, 2]
    assert len(list((output / "candidates").glob("*.json"))) == 3
    assert sorted(path.name for path in corpus.iterdir()) == ["parse-report.json", "segments.jsonl"]

    no_call_model = FakeModelClient([])
    cached = execute_translation_run(spec, output, no_call_model, resume=True)
    assert cached.status == "completed"
    assert cached.model_call_count == 0
    assert cached.cache_hit_unit_count == 3
    assert cached.output_written is False
    assert no_call_model.prompts == []


def test_translation_run_manifest_and_checkpoint_are_content_free(tmp_path):
    _corpus, _plan, requests, spec = build_synthetic_translation_run(tmp_path)
    output = tmp_path / "translation-run"
    responses = [
        json.dumps(valid_response_payload(request), ensure_ascii=False)
        for request in requests
    ]

    result = execute_translation_run(spec, output, FakeModelClient(responses))
    manifest_text = (output / "manifest.json").read_text(encoding="utf-8")
    checkpoint_text = (output / "checkpoint.json").read_text(encoding="utf-8")
    candidate_text = next((output / "candidates").glob("*.json")).read_text(
        encoding="utf-8"
    )

    assert result.status == "completed"
    assert result.to_dict()["side_effects"] == {
        "model_called": True,
        "output_written": True,
        "game_modified": False,
    }
    for metadata_text in (manifest_text, checkpoint_text, result.to_json()):
        assert "Synthetic secret dialogue" not in metadata_text
        assert "合成译文" not in metadata_text
        assert "REQUEST_JSON" not in metadata_text
    assert "translated_text" in candidate_text
    assert "合成译文" in candidate_text


def test_corrupt_candidate_is_rejected_before_resume_calls_model(tmp_path):
    _corpus, _plan, requests, spec = build_synthetic_translation_run(tmp_path)
    output = tmp_path / "translation-run"
    responses = [
        json.dumps(valid_response_payload(request), ensure_ascii=False)
        for request in requests
    ]
    execute_translation_run(spec, output, FakeModelClient(responses))
    candidate_path = next((output / "candidates").glob("*.json"))
    candidate_path.write_bytes(candidate_path.read_bytes() + b" ")
    model = FakeModelClient([])

    with pytest.raises(TranslationCacheError, match="hash") as exc_info:
        execute_translation_run(spec, output, model, resume=True)

    assert exc_info.value.code == "candidate_hash_mismatch"
    assert model.prompts == []


def test_manifest_identity_mismatch_is_rejected_before_model_call(tmp_path):
    _corpus, plan, requests, spec = build_synthetic_translation_run(tmp_path)
    output = tmp_path / "translation-run"
    initialize_translation_run(spec, output)
    changed_requests = tuple(
        build_translation_request(
            batch,
            model_id=plan.config.model_id,
            prompt_version=plan.config.prompt_version,
            terminology_version=plan.config.terminology_version,
            rag_index_id=plan.config.rag_index_id,
            target_language="en-US",
        )
        for batch in plan.batches
    )
    changed_spec = build_translation_run_spec(plan, changed_requests)
    model = FakeModelClient([])

    with pytest.raises(TranslationCacheError, match="manifest") as exc_info:
        execute_translation_run(changed_spec, output, model, resume=True)

    assert exc_info.value.code == "manifest_identity_mismatch"
    assert model.prompts == []


def test_translation_run_output_cannot_overlap_source_or_git_worktree(tmp_path):
    corpus, _plan, _requests, spec = build_synthetic_translation_run(tmp_path)

    with pytest.raises(TranslationCacheError) as source_error:
        initialize_translation_run(spec, corpus / "run")
    with pytest.raises(TranslationCacheError) as git_error:
        initialize_translation_run(spec, "phase4c-run-inside-repo")

    assert source_error.value.code == "unsafe_output"
    assert git_error.value.code == "unsafe_output"
    assert not (corpus / "run").exists()


def test_interrupted_model_call_leaves_atomic_retryable_attempt(tmp_path):
    _corpus, _plan, requests, spec = build_synthetic_translation_run(tmp_path)
    output = tmp_path / "translation-run"

    class InterruptingModel:
        prompts = []
        last_completion_metadata = {}

        def complete(self, prompt, max_new_tokens, **kwargs):
            self.prompts.append(prompt)
            raise KeyboardInterrupt()

    interrupted_model = InterruptingModel()
    with pytest.raises(KeyboardInterrupt):
        execute_translation_run(spec, output, interrupted_model)

    interrupted = load_translation_checkpoint(spec, output)
    assert interrupted["revision"] == 1
    assert interrupted["batches"][0]["status"] == "failed"
    assert interrupted["batches"][0]["reason"] == "attempt_started"
    assert interrupted["batches"][0]["attempts"] == 1
    assert interrupted["batches"][1]["status"] == "pending"

    responses = [
        json.dumps(valid_response_payload(request), ensure_ascii=False)
        for request in requests
    ]
    resumed_model = FakeModelClient(responses)
    resumed = execute_translation_run(spec, output, resumed_model, resume=True)

    assert resumed.status == "completed"
    assert resumed.model_call_count == 2
    assert resumed.retry_count == 1
    assert resumed_model.message_requests == [request.messages for request in requests]


def test_init_run_cli_is_dry_by_default_and_apply_never_calls_model(tmp_path, monkeypatch, capsys):
    corpus = tmp_path / "corpus"
    output = tmp_path / "translation-run"
    write_published_corpus(corpus)

    def fail_if_called(_args):
        raise AssertionError("translation run initialization must not initialize the agent")

    monkeypatch.setattr(cli, "build_agent", fail_if_called)
    arguments = [
        "translate",
        "init-run",
        str(corpus),
        "--output",
        str(output),
        "--model",
        "fake-model-v1",
        "--batch-size",
        "2",
        "--json",
    ]
    dry_code = cli.main(arguments)
    dry_payload = json.loads(capsys.readouterr().out)

    assert dry_code == 0
    assert dry_payload["status"] == "ready"
    assert dry_payload["summary"] == {"batch_count": 2, "unit_count": 3}
    assert dry_payload["side_effects"] == {
        "model_called": False,
        "output_written": False,
        "game_modified": False,
    }
    assert not output.exists()

    apply_code = cli.main([*arguments, "--apply"])
    apply_output = capsys.readouterr().out
    apply_payload = json.loads(apply_output)

    assert apply_code == 0
    assert apply_payload["status"] == "initialized"
    assert apply_payload["run_id"] == dry_payload["run_id"]
    assert apply_payload["side_effects"] == {
        "model_called": False,
        "output_written": True,
        "game_modified": False,
    }
    assert (output / "manifest.json").is_file()
    assert (output / "checkpoint.json").is_file()
    assert (output / "candidates").is_dir()
    assert "Synthetic secret dialogue" not in apply_output


def build_translation_tool_agent(
    tmp_path,
    *,
    approval_policy="auto",
    controller_model=None,
    translation_model=None,
):
    _corpus, _plan, requests, spec = build_synthetic_translation_run(tmp_path)
    output = tmp_path / "translation-tool-run"
    initialize_translation_run(spec, output)
    workspace_root = tmp_path / "agent-workspace"
    workspace_root.mkdir()
    (workspace_root / "README.md").write_text("synthetic agent workspace\n", encoding="utf-8")
    workspace = WorkspaceContext.build(workspace_root)
    session_store = SessionStore(workspace_root / ".nagi" / "sessions")
    translation_model = translation_model or FakeModelClient([])
    agent = Nagi(
        model_client=controller_model or FakeModelClient([]),
        workspace=workspace,
        session_store=session_store,
        approval_policy=approval_policy,
        allowed_tools=("translation_run_status", "translate_game_batch"),
        translation_runs=(TranslationRunBinding(spec, str(output)),),
        translation_model_client=translation_model,
    )
    return agent, translation_model, requests, spec, output


def test_translation_tools_are_allowlisted_and_status_is_content_free(tmp_path):
    agent, translation_model, requests, spec, _output = build_translation_tool_agent(
        tmp_path
    )

    prompt = agent.prompt("Inspect the translation run")
    result = agent.execute_tool("translation_run_status", {"run_id": spec.run_id})
    payload = json.loads(result.content)

    assert "- translation_run_status(" in prompt
    assert "- translate_game_batch(" in prompt
    assert "- run_shell(" not in prompt
    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["read_only"] is True
    assert result.metadata["risk_level"] == "low"
    assert payload["next"] == {
        "request_id": requests[0].request_id,
        "batch_id": requests[0].batch_id,
        "status": "pending",
        "attempts": 0,
    }
    assert payload["side_effects"] == {
        "model_called": False,
        "output_written": False,
        "game_modified": False,
    }
    assert "output_dir" not in payload
    assert "Synthetic secret dialogue" not in result.content
    assert translation_model.prompts == []


def test_approval_denial_causes_zero_model_calls_and_zero_checkpoint_changes(tmp_path):
    agent, translation_model, requests, spec, output = build_translation_tool_agent(
        tmp_path,
        approval_policy="never",
    )
    checkpoint_path = output / "checkpoint.json"
    before = checkpoint_path.read_bytes()

    result = agent.execute_tool(
        "translate_game_batch",
        {
            "run_id": spec.run_id,
            "request_id": requests[0].request_id,
            "max_new_tokens": 512,
        },
    )

    assert result.content == "error: approval denied for translate_game_batch"
    assert result.metadata["tool_error_code"] == "approval_denied"
    assert result.metadata["risk_level"] == "high"
    assert checkpoint_path.read_bytes() == before
    assert translation_model.prompts == []
    assert list((output / "candidates").iterdir()) == []


def test_translate_game_batch_executes_exactly_one_current_request(tmp_path):
    _corpus, _plan, requests, _spec = build_synthetic_translation_run(tmp_path)
    responses = [
        json.dumps(valid_response_payload(request), ensure_ascii=False)
        for request in requests
    ]
    translation_model = FakeModelClient(responses)
    agent, translation_model, requests, spec, output = build_translation_tool_agent(
        tmp_path / "tool-agent",
        translation_model=translation_model,
    )

    first = agent.execute_tool(
        "translate_game_batch",
        {
            "run_id": spec.run_id,
            "request_id": requests[0].request_id,
            "max_new_tokens": 512,
        },
    )
    first_payload = json.loads(first.content)
    first_checkpoint = load_translation_checkpoint(spec, output)

    assert first.metadata["tool_status"] == "ok"
    assert first.metadata["risk_level"] == "high"
    assert first.metadata["read_only"] is False
    assert first_payload["reason"] == "batch_completed"
    assert first_payload["request_id"] == requests[0].request_id
    assert first_payload["summary"]["model_call_count"] == 1
    assert len(translation_model.prompts) == 1
    assert [item["status"] for item in first_checkpoint["batches"]] == [
        "completed",
        "pending",
    ]

    stale = agent.execute_tool(
        "translate_game_batch",
        {
            "run_id": spec.run_id,
            "request_id": requests[0].request_id,
        },
    )
    assert stale.metadata["tool_error_code"] == "invalid_arguments"
    assert "stale" in stale.content
    assert len(translation_model.prompts) == 1

    status = json.loads(
        agent.run_tool("translation_run_status", {"run_id": spec.run_id})
    )
    assert status["next"]["request_id"] == requests[1].request_id
    second = agent.execute_tool(
        "translate_game_batch",
        {
            "run_id": spec.run_id,
            "request_id": requests[1].request_id,
        },
    )
    second_payload = json.loads(second.content)
    final_checkpoint = load_translation_checkpoint(spec, output)

    assert second_payload["status"] == "completed"
    assert second_payload["reason"] == "all_batches_completed"
    assert second_payload["summary"]["model_call_count"] == 1
    assert len(translation_model.prompts) == 2
    assert [item["status"] for item in final_checkpoint["batches"]] == [
        "completed",
        "completed",
    ]
    assert "Synthetic secret dialogue" not in first.content + second.content
    assert "合成译文" not in first.content + second.content


def test_translation_tool_rejects_model_identity_mismatch_without_checkpoint_write(tmp_path):
    translation_model = FakeModelClient(["unused"])
    translation_model.model = "wrong-model"
    agent, translation_model, requests, spec, output = build_translation_tool_agent(
        tmp_path,
        translation_model=translation_model,
    )
    before = (output / "checkpoint.json").read_bytes()

    result = agent.execute_tool(
        "translate_game_batch",
        {
            "run_id": spec.run_id,
            "request_id": requests[0].request_id,
        },
    )

    assert result.metadata["tool_status"] == "error"
    assert "model identity" in result.content
    assert (output / "checkpoint.json").read_bytes() == before
    assert translation_model.prompts == []


def test_agent_translation_tool_trace_contains_only_metadata(tmp_path):
    _corpus, _plan, preview_requests, _spec = build_synthetic_translation_run(tmp_path)
    translation_model = FakeModelClient(
        [json.dumps(valid_response_payload(preview_requests[0]), ensure_ascii=False)]
    )
    controller_outputs = [
        "",
    ]
    controller_model = FakeModelClient(controller_outputs)
    agent, translation_model, requests, spec, output = build_translation_tool_agent(
        tmp_path / "trace-agent",
        controller_model=controller_model,
        translation_model=translation_model,
    )
    controller_model.outputs[:] = [
        json.dumps(
            {
                "placeholder": "replaced below",
            }
        ),
    ]
    controller_model.outputs[:] = [
        '<tool>{"name":"translation_run_status","args":{"run_id":"'
        + spec.run_id
        + '"}}</tool>',
        '<tool>{"name":"translate_game_batch","args":{"run_id":"'
        + spec.run_id
        + '","request_id":"'
        + requests[0].request_id
        + '","max_new_tokens":512}}</tool>',
        "<final>One approved translation batch completed.</final>",
    ]
    translation_model.outputs[:] = [
        json.dumps(valid_response_payload(requests[0]), ensure_ascii=False)
    ]
    secret = "sk-phase4d-secret-123"
    translation_model.last_completion_metadata = {
        "input_tokens": 200,
        "output_tokens": 80,
        "api_key": secret,
    }

    with patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False):
        answer = agent.ask("Inspect and translate exactly one approved batch")

    trace_path = agent.run_store.trace_path(agent.current_task_state)
    trace_text = trace_path.read_text(encoding="utf-8")
    trace_events = [json.loads(line) for line in trace_text.splitlines()]
    tool_events = [event for event in trace_events if event["event"] == "tool_executed"]

    assert answer == "One approved translation batch completed."
    assert [event["name"] for event in tool_events] == [
        "translation_run_status",
        "translate_game_batch",
    ]
    assert tool_events[0]["read_only"] is True
    assert tool_events[1]["read_only"] is False
    assert tool_events[1]["risk_level"] == "high"
    assert tool_events[1]["translation"]["run_id"] == spec.run_id
    assert tool_events[1]["translation"]["request_id"] == requests[0].request_id
    assert tool_events[1]["translation"]["summary"]["model_call_count"] == 1
    assert tool_events[1]["translation"]["completion_metadata"] == {
        "input_tokens": 200,
        "output_tokens": 80,
    }
    assert tool_events[1]["translation"]["artifact_reference_count"] == 2
    assert requests[0].request_id in trace_text
    assert spec.run_id in trace_text
    assert secret not in trace_text
    assert "Synthetic secret dialogue" not in trace_text
    assert "合成译文" not in trace_text
    assert str(output) not in trace_text
    assert len(translation_model.prompts) == 1


def test_translation_tool_provider_failure_remains_retryable(tmp_path):
    agent, translation_model, requests, spec, output = build_translation_tool_agent(
        tmp_path
    )
    arguments = {
        "run_id": spec.run_id,
        "request_id": requests[0].request_id,
        "max_new_tokens": 512,
    }

    failed = agent.execute_tool("translate_game_batch", arguments)
    failed_payload = json.loads(failed.content)
    failed_checkpoint = load_translation_checkpoint(spec, output)

    assert failed.metadata["tool_status"] == "ok"
    assert failed_payload["status"] == "paused"
    assert failed_payload["reason"] == "model_call_failed"
    assert failed_payload["summary"]["model_call_count"] == 1
    assert failed_checkpoint["batches"][0]["status"] == "failed"
    assert failed_checkpoint["batches"][0]["attempts"] == 1
    status = json.loads(
        agent.run_tool("translation_run_status", {"run_id": spec.run_id})
    )
    assert status["next"]["request_id"] == requests[0].request_id
    assert status["next"]["status"] == "failed"
    assert status["next"]["attempts"] == 1

    translation_model.outputs.append(
        json.dumps(valid_response_payload(requests[0]), ensure_ascii=False)
    )
    retried = agent.execute_tool("translate_game_batch", arguments)
    retried_payload = json.loads(retried.content)
    retried_checkpoint = load_translation_checkpoint(spec, output)

    assert retried_payload["reason"] == "batch_completed"
    assert retried_payload["summary"]["retry_count"] == 1
    assert retried_checkpoint["batches"][0]["status"] == "completed"
    assert retried_checkpoint["batches"][0]["attempts"] == 2
