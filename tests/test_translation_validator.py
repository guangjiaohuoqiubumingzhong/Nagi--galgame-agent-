import hashlib
import json
from dataclasses import replace

from nagi.gameio.segments import SegmentInlineToken
from nagi.translation import (
    candidate_acceptance_status,
    build_translation_batch_plan,
    build_translation_candidate,
    validate_translation_candidate,
    validate_translation_candidates,
)

from test_translation_planner import write_published_corpus


def make_unit(tmp_path):
    corpus = tmp_path / "corpus"
    write_published_corpus(corpus)
    plan = build_translation_batch_plan(
        corpus,
        model_id="validator-model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="validator-index-v1",
        batch_size=2,
    )
    return plan.batches[0].units[0]


def make_candidate(unit, text):
    return build_translation_candidate(
        unit,
        text,
        model_id="validator-model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="validator-index-v1",
    )


def test_valid_candidate_is_accepted_and_report_is_content_free(tmp_path):
    unit = make_unit(tmp_path)
    candidate = make_candidate(unit, "合成对话 {hero}")

    report = validate_translation_candidate(unit, candidate)

    assert report.status == "valid"
    assert report.patch_eligible is True
    assert report.expected_token_count == 1
    assert report.actual_token_count == 1
    assert report.preserved_token_count == 1
    assert candidate_acceptance_status(report) == "accepted"
    serialized = report.to_json()
    assert "Synthetic secret dialogue" not in serialized
    assert "合成对话" not in serialized
    assert candidate.translated_text_sha256 in serialized


def test_placeholder_missing_added_duplicate_and_modified_are_errors(tmp_path):
    unit = make_unit(tmp_path)

    cases = {
        "missing": ("合成对话", "token_missing"),
        "added": ("合成对话 {hero} {name}", "token_added"),
        "duplicated": ("合成对话 {hero} {hero}", "token_duplicated"),
        "modified": ("合成对话 {name}", "token_modified"),
    }
    for text, code in cases.values():
        report = validate_translation_candidate(unit, make_candidate(unit, text))
        assert report.status == "invalid"
        assert report.patch_eligible is False
        assert code in report.issue_counts
        assert candidate_acceptance_status(report) == "rejected"


def test_tag_and_escape_streams_preserve_multiset_and_order(tmp_path):
    unit = make_unit(tmp_path)
    unit = replace(
        unit,
        tags=(
            SegmentInlineToken(kind="bracket_tag", raw="[ruby]", char_start=0, char_end=6),
            SegmentInlineToken(kind="escape", raw=r"\n", char_start=7, char_end=9),
            SegmentInlineToken(kind="bracket_tag", raw="[/ruby]", char_start=10, char_end=17),
        ),
    )

    valid = validate_translation_candidate(
        unit, make_candidate(unit, r"译文 {hero} [ruby]\n[/ruby]")
    )
    assert valid.status == "valid"
    assert valid.preserved_token_count == 4

    reordered = validate_translation_candidate(
        unit, make_candidate(unit, r"译文 {hero} [/ruby]\n[ruby]")
    )
    assert reordered.status == "invalid"
    assert reordered.issue_counts == {"token_order_changed": 1}

    modified = validate_translation_candidate(
        unit, make_candidate(unit, r"译文 {hero} [ruby]\t[/ruby]")
    )
    assert modified.status == "invalid"
    assert modified.issue_counts == {"token_modified": 1}


def test_newline_count_is_error_and_line_width_is_review(tmp_path):
    unit = make_unit(tmp_path)
    too_many_lines = validate_translation_candidate(
        unit, make_candidate(unit, "合成对话 {hero}\n第二行")
    )
    assert too_many_lines.status == "invalid"
    assert too_many_lines.patch_eligible is False
    assert too_many_lines.issue_counts == {"newline_count_changed": 1}

    reviewed = validate_translation_candidate(
        unit, make_candidate(unit, "这是一段较长的译文 {hero}"), max_line_chars=4
    )
    assert reviewed.status == "needs_review"
    assert reviewed.patch_eligible is True
    assert reviewed.issue_counts == {"line_length_exceeded": 1}
    assert candidate_acceptance_status(reviewed) == "review"


def test_batch_gate_is_conservative_and_has_no_filesystem_side_effects(tmp_path):
    unit = make_unit(tmp_path)
    second = build_translation_batch_plan(
        tmp_path / "corpus",
        model_id="validator-model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="validator-index-v1",
        batch_size=2,
    ).batches[0].units[1]
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (tmp_path / "corpus").iterdir()
    }

    batch = validate_translation_candidates(
        (unit, second),
        (make_candidate(unit, "合成对话"), make_candidate(second, "合成叙述")),
    )

    assert batch.status == "invalid"
    assert batch.patch_eligible is False
    assert batch.issue_count == 1
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (tmp_path / "corpus").iterdir()
    }
    assert before == after


def test_batch_report_serializes_stably_without_prose(tmp_path):
    unit = make_unit(tmp_path)
    batch = validate_translation_candidates((unit,), (make_candidate(unit, "合成对话 {hero}"),))
    first = json.dumps(batch.to_dict(), ensure_ascii=False, sort_keys=True)
    second = json.dumps(batch.to_dict(), ensure_ascii=False, sort_keys=True)
    assert first == second
    assert "Synthetic secret dialogue" not in first
    assert "合成对话" not in first
