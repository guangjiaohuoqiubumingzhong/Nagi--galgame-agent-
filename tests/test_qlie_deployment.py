import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_qlie_payload import (
    encrypt_normal_file,
    make_literal_bpe,
    write_payload_pack,
    write_synthetic_pe,
)
from test_translation_workflow import settle

from nagi.gameio.deployment import deploy_workflow
from nagi.gameio.deployment_runtime.launch import verified_manifest
from nagi.gameio.qlie import deployment as qlie
from nagi.gameio.qlie.archive import _filename_seed31, inspect_filepack_toc
from nagi.gameio.qlie.corpus import apply_qlie_corpus_plan, build_qlie_corpus_plan
from nagi.gameio.qlie.exporter import apply_script_export_plan, build_script_export_plan
from nagi.gameio.qlie.payload import read_filepack_entry
from nagi.gameio.qlie.repack import encrypt31, repack_scripts
from nagi.translation import (
    TerminologySnapshot,
    TranslationPreviewSpec,
    apply_translation_preview,
    build_translation_batch_plan,
    build_translation_candidate,
    build_translation_patch_preview,
    publish_translation_preview,
)
from nagi.translation_workflow import TranslationWorkflow, TranslationWorkflows
from nagi.webapp import TranslationJob


def make_pack(path, text, key):
    name = "scenario\\main.s"
    original = text.encode("cp932")
    stored = encrypt_normal_file(
        name, make_literal_bpe(original), _filename_seed31(bytes(range(256))), key
    )
    write_payload_pack(path, name, stored, len(original), 1)
    return original


@pytest.mark.parametrize("length", [1, 7, 8, 9, 256, 301])
def test_encrypt31_matches_independent_existing_fixture(length):
    key, data = bytes(range(256)), bytes(i % 256 for i in range(length))
    assert encrypt31("scenario\\main.s", data, 12345, key) == encrypt_normal_file(
        "scenario\\main.s", data, 12345, key
    )


def test_repack_roundtrip_preserves_index_tail_and_source(tmp_path):
    source, target = tmp_path / "source.pack", tmp_path / "output.pack"
    key = bytes(range(256))
    make_pack(source, "「こんにちは。」\r\n", key)
    original = source.read_bytes()
    before = inspect_filepack_toc(source)
    translated = "「简体中文验证：测试成功。」\r\n".encode("utf-16")
    repack_scripts(source, target, {0: translated}, resource_key=key)
    after = inspect_filepack_toc(target)
    assert (
        read_filepack_entry(target, entry_index=0, resource_key=key).data == translated
    )
    assert source.read_bytes() == original
    assert (
        target.read_bytes()[: before.archive.toc_offset]
        == original[: before.archive.toc_offset]
    )
    assert after.entries[0].internal_path == before.entries[0].internal_path
    assert after.entries[0].compression_flag == 0
    # The variable-length record itself changes; auxiliary hash/seed tables don't.
    tail_length = before.archive.toc_size_bytes - (2 + len("scenario\\main.s") * 2 + 28)
    assert (
        original[-28 - tail_length : -28]
        == target.read_bytes()[-28 - tail_length : -28]
    )
    with pytest.raises(ValueError):
        repack_scripts(source, source, {0: translated}, resource_key=key)
    assert source.read_bytes() == original


@pytest.fixture
def translated(tmp_path, monkeypatch):
    game, storage = tmp_path / "game", tmp_path / "translations"
    (game / "GameData").mkdir(parents=True)
    storage.mkdir()
    key = bytes(range(256))
    exe = game / "game.exe"
    write_synthetic_pe(exe, key)
    monkeypatch.setattr(
        qlie, "SUPPORTED_EXECUTABLES", {qlie.digest(exe): "synthetic-only"}
    )
    make_pack(
        game / "GameData/data6.pack", "「こんにちは。」\r\n「また明日。」\r\n", key
    )
    (game / "RuntimeAssets").mkdir()
    (game / "RuntimeAssets/required.bin").write_bytes(b"nonstandard runtime asset")
    (game / "SaveData").mkdir()
    (game / "SaveData/user.sav").write_bytes(b"original save")
    item = TranslationWorkflow("fixture", game, storage, family="QLIE")
    root = Path(item.output_root)
    export, corpus = root / "export", root / "corpus"
    p = build_script_export_plan(game, export)
    assert apply_script_export_plan(p, exe_path=exe).status == "exported"
    p = build_qlie_corpus_plan(export, corpus)
    assert apply_qlie_corpus_plan(p).status == "published"
    plan = build_translation_batch_plan(
        corpus,
        model_id="fixture",
        prompt_version="fixture",
        terminology_version="fixture",
        rag_index_id="fixture",
    )
    units = tuple(u for batch in plan.batches for u in batch.units)
    candidates = tuple(
        build_translation_candidate(
            u,
            "「简体中文测试。」",
            model_id="fixture",
            prompt_version="fixture",
            terminology_version="fixture",
            rag_index_id="fixture",
        )
        for u in units
    )
    spec = TranslationPreviewSpec(
        "fixture",
        plan.plan_id,
        str(corpus),
        plan.segments_sha256,
        plan.parse_report_sha256,
    )
    preview = build_translation_patch_preview(
        units,
        candidates,
        spec,
        current_segments_sha256=spec.segments_sha256,
        current_parse_report_sha256=spec.parse_report_sha256,
        current_source_hashes={u.unit_id: u.source.source_sha256 for u in units},
        expected_candidate_hashes={
            c.unit_id: c.translated_text_sha256 for c in candidates
        },
        terminology_snapshot=TerminologySnapshot(version="fixture"),
    )
    job_root = root / "full-fixture"
    publish_translation_preview(
        preview,
        job_root / "translation-preview",
        include_text=True,
        forbidden_roots=(game,),
    )
    apply_translation_preview(
        job_root / "translation-preview",
        export,
        job_root / "translated-scripts",
        approved=True,
        dry_run=False,
        fallback_encoding="utf-16-le-bom",
        forbidden_roots=(game,),
    )
    extraction = TranslationJob("extract", str(game), "full", str(root))
    extraction.update(export_dir=str(export), corpus_dir=str(corpus))
    job = TranslationJob("translate", str(game), "full", str(job_root))
    job.update(
        status="completed",
        total_units=len(units),
        translated_units=len(units),
        translated_scripts_dir=str(job_root / "translated-scripts"),
    )
    item.extraction_job, item.translation_job, item.job = extraction, job, job
    item.source_kind = "qlie"
    item.model_identity = {"provider": "fixture", "model": "fixture"}
    for stage in ("extract", "translate"):
        item.stages[stage].update(status="completed", progress=1)
    monkeypatch.setattr(qlie.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(qlie.LocaleEmulatorSettings, "snapshot", lambda _: {})
    return item, game, exe


@pytest.mark.parametrize(
    "mode,slot,label",
    [
        ("full", "translation-formal", "启动正式版.cmd"),
        ("partial", "translation-test", "启动测试版.cmd"),
    ],
)
def test_qlie_deploys_verified_current_result_and_keeps_saves(
    translated, mode, slot, label
):
    item, game, _exe = translated
    item.translation_job.mode = mode
    before = {
        p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()
    }
    service = TranslationWorkflows(
        SimpleNamespace(TranslationJob=TranslationJob), deployer=deploy_workflow
    )
    service.items[item.workflow_id] = item
    service.start(item.workflow_id, "deploy")
    payload = settle(item)
    assert payload["stages"]["deploy"]["status"] == "completed", payload
    launcher = Path(payload["deployment_launcher_path"])
    expected_root = (
        Path(item.storage_dir).parent
        / "playable"
        / "QLIE"
        / Path(item.output_root).parent.name
    )
    assert launcher.parent == expected_root / slot
    assert Path(payload["launcher_path"]) == expected_root / label
    assert not list(expected_root.rglob("localized-game-*"))
    assert not list(expected_root.glob(".deploy-*.partial"))
    assert Path(item.output_root).relative_to(Path(item.storage_dir)).parts[0] == "QLIE"
    manifest, copied_exe = verified_manifest(launcher.parent)
    assert manifest["engine"] == "qlie" and manifest["translated_count"] == 2
    assert manifest["display_bridge"] == "qlie_font_bridge.js"
    assert not (launcher.parent / "game/SaveData").exists()
    assert (
        launcher.parent / "game/RuntimeAssets/required.bin"
    ).read_bytes() == b"nonstandard runtime asset"
    pack = launcher.parent / "game/GameData/data6.pack"
    assert "简体中文测试" in read_filepack_entry(
        pack, entry_index=0, exe_path=copied_exe
    ).data.decode("utf-16")
    assert before == {
        p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize(
    "kind", ["translation", "source", "incomplete", "preview", "version"]
)
def test_qlie_refuses_changed_or_incomplete_inputs_before_publish(translated, kind):
    item, game, exe = translated
    if kind == "translation":
        path = (
            Path(item.translation_job.translated_scripts_dir)
            / "resolved/scenario/main.s"
        )
        path.write_bytes(path.read_bytes() + b"altered")
    elif kind == "source":
        path = game / "GameData/data6.pack"
        data = bytearray(path.read_bytes())
        data[0] ^= 1
        path.write_bytes(data)
    elif kind == "incomplete":
        item.translation_job.translated_units -= 1
    elif kind == "preview":
        path = (
            Path(item.translation_job.output_root) / "translation-preview/preview.json"
        )
        path.write_bytes(path.read_bytes() + b" ")
    else:
        exe.write_bytes(exe.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        qlie.deploy_qlie(item, lambda *_: None)
    assert not Path(item.playable_root).exists()


def test_later_patch_cannot_silently_hide_translated_script(translated):
    item, game, _ = translated
    make_pack(game / "GameData/data8.pack", "「別の更新です。」\r\n", bytes(range(256)))
    with pytest.raises(ValueError, match="覆盖"):
        qlie.deploy_qlie(item, lambda *_: None)
    assert not Path(item.playable_root).exists()


def test_qlie_checkpoint_restores_without_api(translated, tmp_path):
    item, _, _ = translated
    service = TranslationWorkflows(
        SimpleNamespace(TranslationJob=TranslationJob),
        state_path=tmp_path / "latest.json",
    )
    service.checkpoint(item)
    receipt = json.loads((tmp_path / "latest.json").read_text())["receipt"]
    restored = service.restore(receipt)
    assert restored.source_kind == "qlie"
    assert restored.translation_job.total_units == 2
    assert (
        restored.translation_job.translated_scripts_dir
        == item.translation_job.translated_scripts_dir
    )
