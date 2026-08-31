from dataclasses import replace

from nagi.translation import (
    CharacterNameRule,
    TerminologyRule,
    TerminologySnapshot,
    build_translation_batch_plan,
    build_translation_candidate,
    candidate_acceptance_status,
    validate_translation_candidate,
    validate_translation_candidates,
)

from test_translation_planner import write_published_corpus


def make_units(tmp_path):
    corpus = tmp_path / "corpus"
    write_published_corpus(corpus)
    plan = build_translation_batch_plan(
        corpus,
        model_id="terminology-model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="terminology-index-v1",
        batch_size=2,
    )
    first = plan.batches[0].units[0]
    second = replace(plan.batches[0].units[1], speaker="Mira")
    return first, second


def make_candidate(unit, text, *, terminology_version="terms-v1"):
    return build_translation_candidate(
        unit,
        text,
        model_id="terminology-model-v1",
        prompt_version="prompt-v1",
        terminology_version=terminology_version,
        rag_index_id="terminology-index-v1",
    )


def make_snapshot(*, version="terms-v1", include_unknown=True):
    terms = [
        TerminologyRule(
            rule_id="term.alpha",
            source="secret",
            target="秘密",
            variants=("机密",),
        )
    ]
    if include_unknown:
        terms.append(
            TerminologyRule(
                rule_id="term.beta",
                source="dialogue",
                target=None,
            )
        )
    return TerminologySnapshot(
        version=version,
        terms=tuple(terms),
        characters=(
            CharacterNameRule(
                rule_id="character.mira",
                speaker="Mira",
                canonical="合成",
                source_names=("Synthetic",),
                variants=("合成君",),
            ),
        ),
    )


def test_snapshot_is_versioned_and_default_serialization_is_content_free():
    snapshot = make_snapshot()
    assert snapshot.snapshot_sha256 == make_snapshot().snapshot_sha256
    payload = snapshot.to_dict()
    assert payload["version"] == "terms-v1"
    assert payload["term_count"] == 2
    assert "secret" not in snapshot.to_json()
    assert "秘密" not in snapshot.to_json()
    assert snapshot.to_dict(include_content=True)["terms"][0]["source"] == "secret"


def test_term_and_canonical_character_name_pass(tmp_path):
    unit, _ = make_units(tmp_path)
    report = validate_translation_candidate(
        unit,
        make_candidate(unit, "合成秘密对话 {hero}"),
        terminology_snapshot=make_snapshot(include_unknown=False),
    )
    assert report.status == "valid"
    assert report.patch_eligible is True
    assert report.terminology_version == "terms-v1"
    assert candidate_acceptance_status(report) == "accepted"
    assert "Synthetic secret dialogue" not in report.to_json()
    assert "秘密" not in report.to_json()


def test_version_mismatch_and_term_mismatch_block_acceptance(tmp_path):
    unit, _ = make_units(tmp_path)
    wrong_version = validate_translation_candidate(
        unit,
        make_candidate(unit, "合成秘密对话 {hero}", terminology_version="terms-old"),
        terminology_snapshot=make_snapshot(),
    )
    assert wrong_version.status == "invalid"
    assert wrong_version.patch_eligible is False
    assert "terminology_version_mismatch" in wrong_version.issue_counts

    wrong_term = validate_translation_candidate(
        unit,
        make_candidate(unit, "合成机密对话 {hero}"),
        terminology_snapshot=make_snapshot(),
    )
    assert wrong_term.status == "invalid"
    assert wrong_term.patch_eligible is False
    assert "term_missing" in wrong_term.issue_counts
    assert "term_variant_used" in wrong_term.issue_counts
    assert candidate_acceptance_status(wrong_term) == "rejected"


def test_unknown_term_and_name_variant_are_reviewable_not_accepted(tmp_path):
    unit, _ = make_units(tmp_path)
    report = validate_translation_candidate(
        unit,
        make_candidate(unit, "合成秘密对话 {hero}"),
        terminology_snapshot=make_snapshot(),
    )
    assert report.status == "needs_review"
    assert report.patch_eligible is True
    assert report.issue_counts == {"unknown_term": 1}
    assert candidate_acceptance_status(report) == "review"

    name_variant = validate_translation_candidate(
        unit,
        make_candidate(unit, "合成君秘密对话 {hero}"),
        terminology_snapshot=TerminologySnapshot(
            version="terms-v1",
            terms=(TerminologyRule("term.alpha", "secret", "秘密"),),
            characters=make_snapshot().characters,
        ),
    )
    assert name_variant.status == "needs_review"
    assert name_variant.patch_eligible is True
    assert "character_name_variant" in name_variant.issue_counts


def test_batch_detects_cross_unit_character_name_inconsistency(tmp_path):
    first, second = make_units(tmp_path)
    snapshot = TerminologySnapshot(
        version="terms-v1",
        terms=(TerminologyRule("term.alpha", "secret", "秘密"),),
        characters=make_snapshot().characters,
    )
    batch = validate_translation_candidates(
        (first, second),
        (
            make_candidate(first, "合成秘密对话 {hero}"),
            make_candidate(second, "合成君叙述"),
        ),
        terminology_snapshot=snapshot,
    )
    assert batch.status == "needs_review"
    assert batch.patch_eligible is True
    assert batch.issue_count >= 2
    assert any(
        "character_name_inconsistent" in report.issue_counts
        for report in batch.reports
    )
