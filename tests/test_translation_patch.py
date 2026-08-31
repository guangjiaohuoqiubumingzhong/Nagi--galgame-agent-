import hashlib

import pytest
from test_translation_planner import write_published_corpus

from nagi.translation import (
    TerminologyRule,
    TerminologySnapshot,
    TranslationPreviewError,
    TranslationPreviewSpec,
    apply_translation_preview,
    build_translation_batch_plan,
    build_translation_candidate,
    build_translation_patch_preview,
    publish_translation_preview,
)
from nagi.translation.patch import _apply_diffs_to_tree


def make_context(tmp_path):
    corpus = tmp_path / "corpus"
    write_published_corpus(corpus)
    plan = build_translation_batch_plan(
        corpus,
        model_id="patch-model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="patch-index-v1",
        batch_size=2,
    )
    units = (plan.batches[0].units[0],)
    candidate = build_translation_candidate(
        units[0],
        "合成秘密对话 {hero}",
        model_id="patch-model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="patch-index-v1",
    )
    spec = TranslationPreviewSpec(
        run_id="trun_v1_patch_demo",
        plan_id=plan.plan_id,
        corpus_dir=str(corpus),
        segments_sha256=plan.segments_sha256,
        parse_report_sha256=plan.parse_report_sha256,
    )
    source_hashes = {unit.unit_id: unit.source.source_sha256 for unit in units}
    snapshot = TerminologySnapshot(
        version="terms-v1",
        terms=(TerminologyRule("term.alpha", "secret", "秘密"),),
    )
    return corpus, units, (candidate,), spec, source_hashes, snapshot


def build_preview(tmp_path, *, candidate=None, **kwargs):
    corpus, units, candidates, spec, source_hashes, snapshot = make_context(tmp_path)
    if candidate is not None:
        candidates = (candidate,)
    return build_translation_patch_preview(
        units,
        candidates,
        spec,
        current_segments_sha256=spec.segments_sha256,
        current_parse_report_sha256=spec.parse_report_sha256,
        current_source_hashes=source_hashes,
        terminology_snapshot=snapshot,
        expected_candidate_hashes={
            item.unit_id: item.translated_text_sha256 for item in candidates
        },
        **kwargs,
    )


def test_preview_has_stable_id_diff_and_content_free_default(tmp_path):
    report = build_preview(tmp_path)
    payload = report.to_dict()
    assert report.status == "valid"
    assert report.patch_eligible is True
    assert payload["summary"]["diff_count"] == 1
    assert "source_text" not in payload["diffs"][0]
    assert "translated_text" not in payload["diffs"][0]
    assert "unified_diff" not in payload["diffs"][0]
    assert "Synthetic secret dialogue" not in report.to_json()
    assert "合成秘密对话" not in report.to_json()
    assert "合成秘密对话" in report.to_json(include_text=True)


def test_structural_or_terminology_error_produces_no_diff_and_no_publishable_preview(
    tmp_path,
):
    corpus, units, candidates, spec, source_hashes, snapshot = make_context(tmp_path)
    invalid_candidate = build_translation_candidate(
        units[0],
        "合成秘密对话",
        model_id="patch-model-v1",
        prompt_version="prompt-v1",
        terminology_version="terms-v1",
        rag_index_id="patch-index-v1",
    )
    report = build_translation_patch_preview(
        units,
        (invalid_candidate,),
        spec,
        current_segments_sha256=spec.segments_sha256,
        current_parse_report_sha256=spec.parse_report_sha256,
        current_source_hashes=source_hashes,
        terminology_snapshot=snapshot,
        expected_candidate_hashes={
            units[0].unit_id: invalid_candidate.translated_text_sha256
        },
    )
    assert report.status == "invalid"
    assert report.patch_eligible is False
    assert report.diffs == ()
    with pytest.raises(TranslationPreviewError, match="not eligible"):
        publish_translation_preview(report, tmp_path / "preview")


def test_identity_mismatches_fail_closed_before_preview(tmp_path):
    corpus, units, candidates, spec, source_hashes, snapshot = make_context(tmp_path)
    common = {
        "current_parse_report_sha256": spec.parse_report_sha256,
        "current_source_hashes": source_hashes,
        "terminology_snapshot": snapshot,
        "expected_candidate_hashes": {
            candidates[0].unit_id: candidates[0].translated_text_sha256
        },
    }
    with pytest.raises(TranslationPreviewError, match="segments hash"):
        build_translation_patch_preview(
            units,
            candidates,
            spec,
            current_segments_sha256="0" * 64,
            **common,
        )
    with pytest.raises(TranslationPreviewError, match="source file hash"):
        build_translation_patch_preview(
            units,
            candidates,
            spec,
            current_segments_sha256=spec.segments_sha256,
            current_source_hashes={units[0].unit_id: "1" * 64},
            **{
                key: value
                for key, value in common.items()
                if key != "current_source_hashes"
            },
        )
    with pytest.raises(TranslationPreviewError, match="candidate hash"):
        build_translation_patch_preview(
            units,
            candidates,
            spec,
            current_segments_sha256=spec.segments_sha256,
            current_source_hashes=source_hashes,
            expected_candidate_hashes={units[0].unit_id: "2" * 64},
            **{
                key: value
                for key, value in common.items()
                if key not in {"current_source_hashes", "expected_candidate_hashes"}
            },
        )


def test_publish_is_external_atomic_sidecar_and_does_not_modify_corpus(tmp_path):
    corpus, units, candidates, spec, source_hashes, snapshot = make_context(tmp_path)
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in corpus.iterdir()
    }
    report = build_translation_patch_preview(
        units,
        candidates,
        spec,
        current_segments_sha256=spec.segments_sha256,
        current_parse_report_sha256=spec.parse_report_sha256,
        current_source_hashes=source_hashes,
        terminology_snapshot=snapshot,
        expected_candidate_hashes={
            item.unit_id: item.translated_text_sha256 for item in candidates
        },
    )
    result = publish_translation_preview(
        report, tmp_path / "published-preview", include_text=True
    )
    assert (tmp_path / "published-preview" / "preview.json").is_file()
    assert (tmp_path / "published-preview" / "manifest.json").is_file()
    assert (
        result.preview_sha256
        == hashlib.sha256(
            (tmp_path / "published-preview" / "preview.json").read_bytes()
        ).hexdigest()
    )
    assert "合成秘密对话" in (
        tmp_path / "published-preview" / "preview.json"
    ).read_text(encoding="utf-8")
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in corpus.iterdir()
    }
    assert before == after


def test_publish_rejects_git_worktree_output(tmp_path):
    report = build_preview(tmp_path)
    with pytest.raises(TranslationPreviewError, match="Git worktree"):
        publish_translation_preview(report, "preview-under-repo")


def make_published_apply_fixture(tmp_path):
    corpus, units, candidates, spec, source_hashes, snapshot = make_context(tmp_path)
    report = build_translation_patch_preview(
        units,
        candidates,
        spec,
        current_segments_sha256=spec.segments_sha256,
        current_parse_report_sha256=spec.parse_report_sha256,
        current_source_hashes=source_hashes,
        terminology_snapshot=snapshot,
        expected_candidate_hashes={
            item.unit_id: item.translated_text_sha256 for item in candidates
        },
    )
    preview_dir = tmp_path / "published-preview"
    publish_translation_preview(report, preview_dir, include_text=True)
    source_root = tmp_path / "source-scripts"
    source_path = source_root / "synthetic" / "scenario" / "1.s"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(units[0].source_text + "\n", encoding="utf-8")
    return corpus, preview_dir, source_root, units[0].source_text


def test_apply_defaults_to_read_only_dry_run(tmp_path):
    corpus, preview_dir, source_root, source_text = make_published_apply_fixture(
        tmp_path
    )
    before = (source_root / "synthetic" / "scenario" / "1.s").read_bytes()

    result = apply_translation_preview(
        preview_dir,
        source_root,
        tmp_path / "applied",
    )

    assert result.status == "dry_run"
    assert result.output_dir is None
    assert result.changed_file_count == 1
    assert not (tmp_path / "applied").exists()
    assert (source_root / "synthetic" / "scenario" / "1.s").read_bytes() == before
    assert source_text.encode("utf-8") in before


def test_apply_requires_explicit_approval_and_publishes_new_tree(tmp_path):
    corpus, preview_dir, source_root, source_text = make_published_apply_fixture(
        tmp_path
    )
    target = tmp_path / "applied"

    with pytest.raises(TranslationPreviewError, match="explicit approval"):
        apply_translation_preview(preview_dir, source_root, target, dry_run=False)
    assert not target.exists()

    result = apply_translation_preview(
        preview_dir,
        source_root,
        target,
        approved=True,
        dry_run=False,
    )
    translated = (target / "synthetic" / "scenario" / "1.s").read_text(encoding="utf-8")
    assert result.status == "applied"
    assert result.changed_file_count == 1
    assert source_text not in translated
    assert "合成秘密对话 {hero}" in translated
    assert (target / "translation-apply.json").is_file()
    assert (source_root / "synthetic" / "scenario" / "1.s").read_text(
        encoding="utf-8"
    ) == source_text + "\n"


def test_apply_rejects_tampered_preview_and_changed_corpus(tmp_path):
    corpus, preview_dir, source_root, _ = make_published_apply_fixture(tmp_path)
    preview_path = preview_dir / "preview.json"
    original = preview_path.read_text(encoding="utf-8")
    preview_path.write_text(original + "\n", encoding="utf-8")
    with pytest.raises(TranslationPreviewError, match="does not match"):
        apply_translation_preview(preview_dir, source_root, tmp_path / "tampered")

    preview_path.write_bytes(original.encode("utf-8"))
    parse_report = corpus / "parse-report.json"
    parse_report.write_text(
        parse_report.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    with pytest.raises(TranslationPreviewError, match="parse report hash"):
        apply_translation_preview(preview_dir, source_root, tmp_path / "changed-corpus")


def test_apply_rejects_missing_source_range_and_allows_repeated_text(tmp_path):
    corpus, preview_dir, source_root, source_text = make_published_apply_fixture(
        tmp_path
    )
    source_path = source_root / "synthetic" / "scenario" / "1.s"
    source_path.write_text("different\n", encoding="utf-8")
    with pytest.raises(TranslationPreviewError, match="does not identify"):
        apply_translation_preview(preview_dir, source_root, tmp_path / "missing")

    source_path.write_text(source_text + "\n" + source_text + "\n", encoding="utf-8")
    result = apply_translation_preview(
        preview_dir, source_root, tmp_path / "repeated-text"
    )
    assert result.status == "dry_run"


def test_apply_rejects_source_or_git_overlapping_targets(tmp_path):
    _, preview_dir, source_root, _ = make_published_apply_fixture(tmp_path)
    with pytest.raises(TranslationPreviewError, match="overlap source"):
        apply_translation_preview(preview_dir, source_root, source_root / "child")
    with pytest.raises(TranslationPreviewError, match="Git worktree"):
        apply_translation_preview(preview_dir, source_root, "apply-under-repo")


def test_apply_can_convert_cp932_script_to_utf16_when_chinese_is_not_encodable(
    tmp_path,
):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_path = source_root / "scenario" / "main.s"
    source_path.parent.mkdir(parents=True)
    source_text = "日本語"
    translated_text = "简体中文"
    source_path.write_bytes((source_text + "\r\n").encode("cp932"))
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    diffs = (
        {
            "unit_id": "unit-1",
            "segment_id": "segment-1",
            "kind": "dialogue",
            "output_path": "scenario/main.s",
            "archive_name": "data.pack",
            "internal_path": "scenario\\main.s",
            "speaker_sha256": None,
            "byte_start": 0,
            "byte_end": len(source_text.encode("cp932")),
            "encoding": "cp932",
            "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "source_text_sha256": digest(source_text),
            "translated_text_sha256": digest(translated_text),
            "source_text": source_text,
            "translated_text": translated_text,
            "unified_diff": "",
        },
    )

    changed = _apply_diffs_to_tree(
        source_root,
        target_root,
        diffs,
        fallback_encoding="utf-16-le-bom",
    )

    output = (target_root / "scenario" / "main.s").read_bytes()
    assert changed == 1
    assert output.startswith(b"\xff\xfe")
    assert translated_text in output.decode("utf-16")


def test_apply_uses_byte_ranges_when_source_text_is_repeated(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_path = source_root / "scenario" / "main.s"
    source_path.parent.mkdir(parents=True)
    source_text = "【深見】\r\n台詞\r\n【深見】\r\n"
    source_bytes = b"\xff\xfe" + source_text.encode("utf-16-le")
    source_path.write_bytes(source_bytes)
    repeated = "【深見】"
    translated = "【深见】"
    first = source_bytes.find(repeated.encode("utf-16-le"))
    second = source_bytes.find(repeated.encode("utf-16-le"), first + 1)
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    diffs = (
        {
            "unit_id": "unit-2",
            "segment_id": "segment-2",
            "kind": "speaker",
            "output_path": "scenario/main.s",
            "archive_name": "data.pack",
            "internal_path": "scenario\\main.s",
            "speaker_sha256": None,
            "byte_start": second,
            "byte_end": second + len(repeated.encode("utf-16-le")),
            "encoding": "utf-16-le-bom",
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "source_text_sha256": digest(repeated),
            "translated_text_sha256": digest(translated),
            "source_text": repeated,
            "translated_text": translated,
            "unified_diff": "",
        },
    )

    changed = _apply_diffs_to_tree(source_root, target_root, diffs)

    output = (target_root / "scenario" / "main.s").read_bytes().decode("utf-16")
    assert changed == 1
    assert output == "【深見】\r\n台詞\r\n【深见】\r\n"
