import json
from pathlib import Path

import pytest
from test_opening_selection import story_archive
from test_translation_planner import make_segment
from test_yuris import synthetic_archive

from nagi.gameio import yuris
from nagi.gameio.deployment import yuris_assets
from nagi.gameio.segments import segments_to_jsonl
from nagi.translation import build_translation_batch_plan
from nagi.translation.context import (
    TranslationContextBuilder,
    prepare_translation_requests,
)
from nagi.translation.yuris import translate
from nagi.webapp import TranslationJob


@pytest.mark.parametrize("count", [9, 49, 75])
def test_partial_is_opening_story_and_preserves_unselected_records(tmp_path, count):
    game = tmp_path / "game"
    (game / "pac").mkdir(parents=True)
    source = story_archive(count=count)
    (game / "pac/ysbin.ypf").write_bytes(source)
    job = TranslationJob("partial", str(game), "partial", str(tmp_path / "partial"),
                         model_config={"provider": "fake", "model": "fake"})
    _, corpus = yuris.extract_game(game, tmp_path / "extract", job)
    units = json.loads((corpus / "texts.json").read_text(encoding="utf-8"))
    from nagi.gameio.yuris_opening import select_opening

    entries = yuris.archive_entries(source)
    table, key, _ = yuris.catalog(entries)
    expected, opening = select_opening(entries, table, key, units)
    selected_ids = {unit["id"] for unit in expected}
    requests = []

    class Client:
        def complete(self, messages, **kwargs):
            rows = json.loads(messages[1]["content"])["texts"]
            requests.extend(rows)
            return json.dumps({"translations": [{"id": row["id"], "text": "已翻译中文"} for row in rows]})

    translate(job, corpus, Client, workers=1)
    assert requests == [{"id": unit["id"], "text": unit["text"]} for unit in expected]
    assert job.total_units == job.translated_units == min(50, count)
    assert job.status == "completed"
    result, packed, mapping = yuris_assets(job.output_root, game)
    assert result["source_text_count"] == len(units) and result["selection_limit"] == 50
    assert result["opening_selection"] == opening
    decoded = json.loads(mapping)["glyphs"]
    table, key, _ = yuris.catalog(yuris.archive_entries(source))
    patched_texts = [text for entry in yuris.archive_entries(packed)
                     if entry["content"][:4] == b"YSTB"
                     for _, _, text, _ in yuris.text_fields(yuris.parse_script(entry["content"], key, table)[0])]
    assert len(patched_texts) == len(units)
    for index, value in enumerate(patched_texts):
        text = "".join(decoded.get(char, char) for char in value)
        assert text == ("已翻译中文" if units[index]["id"] in selected_ids else units[index]["text"])
    assert (game / "pac/ysbin.ypf").read_bytes() == source
    assert json.loads((corpus / "texts.json").read_text(encoding="utf-8")) == units

    translate(job, corpus, lambda: pytest.fail("Resume must reuse the exact partial API receipts"))
    assert json.loads((Path(job.output_root) / "translations.json").read_text(encoding="utf-8")) == {
        unit["id"]: "已翻译中文" for unit in expected
    }
    before = {path: path.read_bytes() for path in Path(job.output_root).rglob("*.json")}
    job.mode = "full"
    with pytest.raises(ValueError, match="plan differs"):
        translate(job, corpus, lambda: pytest.fail("Scope changes need a new output directory"))
    assert all(path.read_bytes() == data for path, data in before.items())


def test_partial_archive_cannot_translate_unknown_or_missing_ids():
    source = synthetic_archive(count=70)
    units = yuris.catalog(yuris.archive_entries(source))[2]
    expected = [unit["id"] for unit in units[:50]]
    translated = {identifier: "中文" for identifier in expected}
    for invalid in ({**translated, units[50]["id"]: "extra"}, {**translated, "unknown": "extra"}, {}):
        with pytest.raises(ValueError, match="catalog"):
            yuris.patched_archive(source, invalid, selected_ids=expected)
    with pytest.raises(ValueError, match="catalog"):
        yuris.patched_archive(source, translated)  # Full mode stays strict.


def test_unresolved_opening_fails_before_any_paid_request(tmp_path):
    game = tmp_path / "game"
    (game / "pac").mkdir(parents=True)
    (game / "pac/ysbin.ypf").write_bytes(story_archive(next_target="MISSING"))
    job = TranslationJob("bad", str(game), "partial", str(tmp_path / "partial"),
                         model_config={"provider": "fake", "model": "fake"})
    _, corpus = yuris.extract_game(game, tmp_path / "extract", job)
    with pytest.raises(ValueError, match="开场"):
        translate(job, corpus, lambda: pytest.fail("No name or body API before scope validation"))


def test_legacy_partial_resume_and_deploy_preserve_paid_scope(tmp_path):
    from nagi.translation.yuris import PROMPT, batches

    game = tmp_path / "game"
    (game / "pac").mkdir(parents=True)
    source = story_archive()
    (game / "pac/ysbin.ypf").write_bytes(source)
    job = TranslationJob("legacy", str(game), "partial", str(tmp_path / "partial"),
                         model_config={"provider": "fake", "model": "fake"})
    _, corpus = yuris.extract_game(game, tmp_path / "extract", job)
    units = yuris.catalog(yuris.archive_entries(source))[2]
    selected = units[:50]
    identity = {"engine": yuris.ENGINE, "provider": "fake", "model": "fake",
                "archive_sha256": yuris.sha(source), "prompt_sha256": yuris.sha(PROMPT.encode()),
                "units_sha256": yuris.sha(json.dumps(selected, ensure_ascii=False, sort_keys=True).encode()),
                "text_count": len(selected), "batch_count": len(list(batches(selected))),
                "content_filter": False, "translation_quality_check": False,
                "mode": "partial", "source_text_count": len(units), "selection_limit": 50}
    yuris.save_json(Path(job.output_root) / "translation-plan.json", identity)
    requested = []

    class Client:
        def complete(self, messages, **kwargs):
            rows = json.loads(messages[1]["content"])["texts"]
            requested.extend(row["id"] for row in rows)
            return json.dumps({"translations": [{"id": row["id"], "text": "原任务译文"} for row in rows]})

    translate(job, corpus, Client, workers=1)
    assert requested == [unit["id"] for unit in selected]
    result, _, _ = yuris_assets(job.output_root, game)
    assert "opening_selection" not in result
    translate(job, corpus, lambda: pytest.fail("Completed legacy receipts must be reused"))


def test_qlie_partial_never_backfills_from_records_after_50(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    segments = [make_segment(i, f"文本 {i}", kind="dialogue" if i else "control",
                             translatable=bool(i)) for i in range(70)]
    payload = segments_to_jsonl(segments).encode()
    (corpus / "segments.jsonl").write_bytes(payload)
    yuris.save_json(corpus / "parse-report.json", {
        "status": "published", "summary": {"segment_count": 70, "unknown_count": 0},
        "artifacts": {"segments": {"sha256": yuris.sha(payload)}},
    })
    plan, requests, _ = prepare_translation_requests(
        corpus, model_id="fake", limit=50, source_limit=50,
    )
    assert plan.status == "ready" and plan.selected_unit_count == 49
    assert {unit.source_text for batch in plan.batches for unit in batch.units} == {f"文本 {i}" for i in range(1, 50)}
    assert sum(len(request.units) for request in requests) == 49
    full = build_translation_batch_plan(corpus, model_id="fake")
    assert full.selected_unit_count == 69
    assert "source_limit" not in full.config.to_dict()  # Preserve existing full-plan identities.
    scoped = build_translation_batch_plan(corpus, model_id="fake", source_limit=50)
    with pytest.raises(ValueError, match="complete validated corpus"):
        TranslationContextBuilder(scoped)
