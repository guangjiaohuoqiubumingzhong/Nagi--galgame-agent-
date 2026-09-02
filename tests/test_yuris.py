import json
import struct
from pathlib import Path

import pytest

from nagi.gameio import yuris
from nagi.translation.yuris import batches, complete_batch, parse_response, translate
from nagi.webapp import TranslationJob


def synthetic_archive(count=35, suffix="", choice_expression=False):
    table = b"YSCM" + struct.pack("<III", 479, 3, 0)
    table += b"WORD\0\1STR\0\0\0GOSUB\0\2#\0\0\0PSTR\0\0\0END\0\0"
    code, arguments, resources = bytearray(), bytearray(), bytearray()
    source = [f"話者「これは本文{i}です。{suffix}」" for i in range(count)]
    for text in source:
        code.extend(struct.pack("<BBH", 0, 1, 0))
        payload = text.encode("cp932")
        arguments.extend(struct.pack("<HBBII", 0, 0, 0, len(payload), len(resources)))
        resources.extend(payload)
    code.extend(struct.pack("<BBH", 1, 2, 0))
    for identifier, text in enumerate(('"ES.SEL.SET"', '"選択肢です"')):
        payload = text.encode("cp932")
        payload = struct.pack("<BH", 0x4D, len(payload)) + payload
        if identifier == 1 and choice_expression:
            payload = choice_bytecode()
        arguments.extend(
            struct.pack("<HBBII", identifier, 3, 0, len(payload), len(resources))
        )
        resources.extend(payload)
    code.extend(struct.pack("<BBH", 2, 0, 0))
    lines = bytes(len(code))
    key = bytes.fromhex("d36fac96")
    script = struct.pack(
        "<4s7I",
        b"YSTB",
        479,
        len(code) // 4,
        len(code),
        len(arguments),
        len(resources),
        len(lines),
        0,
    )
    script += b"".join(
        yuris.crypt(part, key) for part in (code, arguments, resources, lines)
    )
    entries = [
        {
            "name": name,
            "raw_name": name.encode("cp932"),
            "kind": 0,
            "compressed": 1,
            "content": content,
        }
        for name, content in [
            ("ysbin/ysc.ybn", table),
            ("ysbin/yst00001.ybn", script),
            ("ysbin/opaque.bin", b"keep intact"),
        ]
    ]
    return bytes(yuris.pack_archive(entries))


def choice_bytecode():
    def string(value):
        raw = ('"' + value + '"').encode("cp932")
        return struct.pack("<BH", 0x4D, len(raw)) + raw

    # Variables and operators must survive replacement of both text operands.
    return (
        string("選択肢：")
        + b"\x56\x03\x00$\x08\x00\x2b\x00\x00"
        + string("を選びます")
        + b"\x2b\x00\x00"
        + string("icon.png")
        + b"\x2b\x00\x00"
    )


def test_expression_fragments_reach_api_and_preserve_variables(tmp_path):
    original = synthetic_archive(choice_expression=True)
    entries = yuris.archive_entries(original)
    table, key, units = yuris.catalog(entries)
    fragments = [unit for unit in units if unit["representation"] == "expression"]
    assert [unit["text"] for unit in fragments] == ["選択肢：", "を選びます"]
    assert len(units) == 37 and len({unit["id"] for unit in units}) == 37
    assert {unit["id"] for batch in batches(units) for unit in batch} == {
        unit["id"] for unit in units
    }
    translations = {unit["id"]: unit["text"] for unit in units}
    replacements = ["选择：", "确定选取"]
    translations.update(zip((unit["id"] for unit in fragments), replacements))
    packed, mapping = yuris.patched_archive(original, translations)
    instructions, _ = yuris.parse_script(
        yuris.archive_entries(packed)[1]["content"], key, table
    )
    choice = next(i for i in instructions if i["command"] == "GOSUB")["fields"][1][
        "blob"
    ]
    actual = yuris.expression_literals(choice)
    assert ["".join(mapping.get(c, c) for c in text) for _, _, text in actual] == [
        *replacements,
        "icon.png",
    ]

    def non_strings(blob):
        result, cursor = bytearray(), 0
        for start, end, _ in yuris.expression_literals(blob):
            result.extend(blob[cursor:start])
            cursor = end
        return bytes(result) + blob[cursor:]

    assert non_strings(choice) == non_strings(choice_bytecode())
    assert yuris.expression_literals(b"\x56\x03\x00$\x08\x00") == []
    with pytest.raises(ValueError, match="Truncated"):
        yuris.expression_literals(choice_bytecode()[:-1])


def test_catalog_keeps_all_dialogue_and_choices():
    entries = yuris.archive_entries(synthetic_archive())
    _, key, units = yuris.catalog(entries)
    assert key.hex() == "d36fac96"
    assert len(units) == 36
    for index in (9, 18, 21, 34):
        assert f"本文{index}" in units[index]["text"]
    assert units[-1]["text"] == "選択肢です"


def test_patch_preserves_control_flow_and_writes_chinese_without_loss():
    original = synthetic_archive()
    entries = yuris.archive_entries(original)
    table, key, units = yuris.catalog(entries)
    translated = {unit["id"]: "这是汉化译文，继续开始选择游戏。" for unit in units}
    packed, mapping = yuris.patched_archive(original, translated)
    actual = yuris.archive_entries(packed)
    assert actual[-1]["content"] == b"keep intact"
    old = yuris.parse_script(entries[1]["content"], key, table)[1]
    new = yuris.parse_script(actual[1]["content"], key, table)[1]
    assert old[0] == new[0] and old[3] == new[3]
    assert new[2].startswith(old[2])
    for _instruction, _field, text, _representation in yuris.text_fields(
        yuris.parse_script(actual[1]["content"], key, table)[0]
    ):
        assert "".join(mapping.get(char, char) for char in text) == next(
            iter(translated.values())
        )
    with pytest.raises(ValueError, match="complete"):
        yuris.patched_archive(original, {units[0]["id"]: "不能默默跳过"})


@pytest.mark.parametrize("choice_expression", [False, True])
def test_every_extracted_record_reaches_api_and_resume_does_not_repay(
    tmp_path, choice_expression
):
    game, output = tmp_path / "game", tmp_path / "extract"
    (game / "pac").mkdir(parents=True)
    (game / "game.exe").write_bytes(b"MZ synthetic YU-RIS executable")
    (game / "pac/ysbin.ypf").write_bytes(
        synthetic_archive(count=120, choice_expression=choice_expression)
    )
    job = TranslationJob(
        "test",
        str(game),
        "full",
        str(tmp_path / "translated"),
        model_config={"provider": "fake", "model": "fake"},
    )
    _, corpus = yuris.extract_game(game, output, job)
    received = []

    class Client:
        def complete(self, messages, max_new_tokens):
            assert [row["role"] for row in messages] == ["system", "user"]
            assert max_new_tokens == 8192
            rows = json.loads(messages[1]["content"])["texts"]
            received.extend(rows)
            return json.dumps(
                {
                    "translations": [
                        {"id": row["id"], "text": "中文汉化游戏正文"} for row in rows
                    ]
                }
            )

    translate(job, corpus, Client, workers=2)
    expected = json.loads((corpus / "texts.json").read_text(encoding="utf-8"))
    assert sorted(received, key=lambda row: row["id"]) == sorted(
        [{"id": row["id"], "text": row["text"]} for row in expected],
        key=lambda row: row["id"],
    )
    assert job.translated_units == len(expected) == 121 + int(choice_expression)
    assert job.status == "completed"
    assert not (Path(job.output_root) / "translated.ypf").exists()
    assert not list(output.rglob("*.ypf")) and not list(output.rglob("*.ybn"))
    assert not list(Path(job.output_root).rglob("*.request.json"))
    assert not list(Path(job.output_root).rglob("*.received.json"))
    assert (Path(job.output_root) / "translations.json").is_file()
    translate(job, corpus, lambda: pytest.fail("Completed batches must be reused"))
    receipt_path = Path(job.output_root) / "api-batches/00000.response.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["request_sha256"] = "modified"
    yuris.save_json(receipt_path, receipt)
    with pytest.raises(ValueError, match="exact batch"):
        translate(
            job,
            corpus,
            lambda: pytest.fail("Invalid cache must not trigger a paid API call"),
        )
    expected[0]["text"] = "changed source text"
    yuris.save_json(corpus / "texts.json", expected)
    with pytest.raises(ValueError, match="complete source archive"):
        translate(job, corpus, lambda: pytest.fail("Modified source must not be sent"))


def test_glyph_mapping_reserves_decrypted_source_and_preserves_cp932_aliases():
    source = synthetic_archive(suffix="\ue000\ue001")
    entries = yuris.archive_entries(source)
    table, key, units = yuris.catalog(entries)
    text = "这汉〜−"
    translations = {unit["id"]: text for unit in units}
    encoding = yuris.glyph_map(entries, translations)
    assert not {"\ue000", "\ue001"}.intersection(encoding.values())
    packed, mapping = yuris.patched_archive(source, translations)
    instructions, _ = yuris.parse_script(
        yuris.archive_entries(packed)[1]["content"], key, table
    )
    words = [
        field["blob"].decode("cp932")
        for instruction in instructions
        if instruction["command"] == "WORD"
        for field in instruction["fields"]
    ]
    assert len(words) == 35
    assert all(
        "".join(mapping.get(char, char) for char in word) == text for word in words
    )


def test_full_game_glyphs_can_exceed_pilot_private_use_capacity():
    entries = yuris.archive_entries(synthetic_archive())
    text = "".join(map(chr, range(0x3400, 0x3400 + 2000)))
    encoding = yuris.glyph_map(entries, {"all": text})
    assert len(encoding) == 2000
    assert len(set(encoding.values())) == 2000
    assert any(ord(glyph) < 0xE000 for glyph in encoding.values())
    assert all(
        glyph.encode("cp932").decode("cp932") == glyph for glyph in encoding.values()
    )


def test_only_transport_shape_not_translation_accuracy_is_a_gate():
    units = [{"id": "one", "text": "原文"}]
    assert parse_response(
        '{"translations":[{"id":"one","text":"任意译文"}]}', units
    ) == {"one": "任意译文"}
    with pytest.raises(ValueError, match="incomplete"):
        parse_response('{"translations":[]}', units)
    assert (
        sum(map(len, batches([{"id": str(i), "text": "全文"} for i in range(120)])))
        == 120
    )


def test_bad_json_splits_and_reuses_successful_child_receipts(tmp_path):
    batch = [{"id": str(i), "text": "原文"} for i in range(4)]
    job = TranslationJob("test", "game", "full", str(tmp_path))
    requests = []
    fail_right = True

    class Client:
        def complete(self, messages, **_kwargs):
            rows = json.loads(messages[1]["content"])["texts"]
            requests.append([r["id"] for r in rows])
            if len(rows) > 2:
                return '{"translations":['
            if fail_right and rows[0]["id"] == "2":
                raise RuntimeError("temporary provider failure")
            return json.dumps(
                {"translations": [{"id": r["id"], "text": "译文"} for r in rows]}
            )

    with pytest.raises(RuntimeError, match="provider"):
        complete_batch(job, Client, tmp_path, "00000", batch)
    assert requests == [
        ["0", "1", "2", "3"],
        ["0", "1", "2", "3"],
        ["0", "1"],
        ["2", "3"],
    ]
    fail_right = False
    result = complete_batch(job, Client, tmp_path, "00000", batch)
    assert set(result) == {"0", "1", "2", "3"}
    assert len(requests) == 5 and requests[-1] == ["2", "3"]
    receipt = json.loads((tmp_path / "00000.response.json").read_text(encoding="utf-8"))
    assert receipt["assembled_from"] == ["00000.part0", "00000.part1"]
    complete_batch(
        job, lambda: pytest.fail("No extra paid request"), tmp_path, "00000", batch
    )


def test_single_record_format_failure_is_bounded(tmp_path):
    calls = []

    class Client:
        def complete(self, *args, **kwargs):
            calls.append(1)
            return '{"translations":[]}'

    job = TranslationJob("test", "game", "full", str(tmp_path))
    with pytest.raises(ValueError, match="bounded"):
        complete_batch(job, Client, tmp_path, "0", [{"id": "0", "text": "原文"}])
    assert len(calls) == 2 and not (tmp_path / "0.response.json").exists()


@pytest.mark.parametrize("mode", ["full", "partial"])
def test_yuris_restart_restores_without_paid_calls_or_plaintext_credentials(
    tmp_path, monkeypatch, mode
):
    from test_translation_workflow import settle

    from nagi import webapp
    from nagi.translation_workflow import TranslationWorkflows

    game, storage = tmp_path / "game", tmp_path / "storage"
    (game / "pac").mkdir(parents=True)
    (game / "game.exe").write_bytes(b"MZ synthetic YU-RIS executable")
    storage.mkdir()
    (game / "pac/ysbin.ypf").write_bytes(synthetic_archive())
    config = {"provider": "fake", "model": "fake", "api_key": "never-persist-this-key"}
    monkeypatch.setattr(webapp, "_configured_model", lambda: config)
    monkeypatch.setattr(
        webapp,
        "_model_client",
        lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    state_path = tmp_path / "latest.json"
    service = TranslationWorkflows(webapp, state_path=state_path)
    item = service.create(str(game), str(storage))
    settle(item)
    service.start(item.workflow_id, "translate", mode=mode, confirmed=True)
    assert settle(item)["stages"]["translate"]["status"] == "failed"
    receipt = Path(json.loads(state_path.read_text(encoding="utf-8"))["receipt"])
    assert config["api_key"] not in receipt.read_text(encoding="utf-8")
    restored_service = TranslationWorkflows(webapp, state_path=state_path)
    monkeypatch.setattr(
        webapp,
        "_configured_model",
        lambda: pytest.fail("Restore must not load credentials or call API"),
    )
    restored = restored_service.restore(receipt)
    assert restored.active is None and restored.translation_job.model_config is None
    assert restored.translation_job.mode == mode
    assert restored.translation_job.output_root == item.translation_job.output_root
    with pytest.raises(ValueError, match="费用"):
        restored_service.start(restored.workflow_id, "translate", mode=mode)
    monkeypatch.setattr(
        webapp, "_configured_model", lambda: {**config, "model": "different"}
    )
    with pytest.raises(ValueError, match="原任务"):
        restored_service.start(restored.workflow_id, "translate", mode=mode, confirmed=True)
    state = json.loads(receipt.read_text(encoding="utf-8"))
    state["stages"]["translate"]["status"] = "running"
    yuris.save_json(receipt, state)
    assert (
        restored_service.restore(receipt).stages["translate"]["status"] == "cancelled"
    )


@pytest.mark.parametrize("mode", ["full", "partial"])
def test_workbench_three_stages_deploy_current_result(tmp_path, monkeypatch, mode):
    from test_translation_workflow import settle

    from nagi import webapp
    from nagi.gameio import deployment
    from nagi.gameio.deployment_runtime.launch import verified_manifest
    from nagi.translation_workflow import TranslationWorkflows
    from scripts.audit_yuris_run import audit

    game, storage = tmp_path / "Ｍ．Ｃ．催眠研究", tmp_path / "storage"
    (game / "pac").mkdir(parents=True)
    (game / "movies").mkdir()
    (game / "save").mkdir()
    storage.mkdir()
    (game / "pac/ysbin.ypf").write_bytes(synthetic_archive(count=75))
    (game / "pac/bgm.ypf").write_bytes(b"title-specific resource")
    (game / "movies/opening.dat").write_bytes(b"runtime movie")
    (game / "M.C.催眠研究.exe").write_bytes(b"test executable - never run")
    (game / "エンジン設定.exe").write_bytes(b"configuration helper")
    (game / "save/original.sav").write_bytes(b"original save")
    original_files = {
        path.relative_to(game): path.read_bytes()
        for path in game.rglob("*")
        if path.is_file()
    }
    from test_locale_emulator import make_locale

    from nagi.locale_emulator import LocaleEmulatorSettings

    locale = make_locale(tmp_path / "external LE")
    settings = LocaleEmulatorSettings(tmp_path / "settings.json")
    settings.save(str(locale))
    monkeypatch.setattr(webapp, "_locale_settings", lambda: settings)
    monkeypatch.setattr(deployment.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        webapp,
        "_configured_model",
        lambda: {"provider": "fake", "model": "fake", "api_key": "test"},
    )
    requests = []

    class Client:
        def complete(self, messages, **_kwargs):
            rows = json.loads(messages[1]["content"])["texts"]
            requests.extend(rows)
            return json.dumps(
                {
                    "translations": [
                        {"id": row["id"], "text": "全部中文汉化"} for row in rows
                    ]
                }
            )

    monkeypatch.setattr(webapp, "_model_client", lambda config: Client())
    service = TranslationWorkflows(webapp, deployer=deployment.deploy_workflow)
    item = service.create(str(game), str(storage))
    assert settle(item)["source_kind"] == yuris.ENGINE
    service.start(item.workflow_id, "translate", mode=mode, confirmed=True)
    translated_state = settle(item)
    assert translated_state["stages"]["translate"]["status"] == "completed", translated_state["stages"]["translate"]
    expected_count = 50 if mode == "partial" else 76
    assert item.translation_job.mode == mode and len(requests) == expected_count
    yuris.save_json(Path(item.output_root) / "workflow.json", item.public_dict())
    evidence = audit(item.output_root)
    assert evidence["source_texts"] == 76
    assert evidence["selected_texts"] == evidence["api_result_texts"] == expected_count
    assert evidence["successful_api_leaf_batches"] == 2
    assert evidence["scripts_control_flow_unchanged"] == 1
    service.start(item.workflow_id, "deploy")
    result = settle(item)
    assert result["stages"]["deploy"]["status"] == "completed", result
    manifest, executable = verified_manifest(Path(result["deployment_launcher_path"]).parent)
    assert Path(result["deployment_launcher_path"]).parent == Path(item.playable_root)
    assert Path(item.playable_root).name == ("translation-formal" if mode == "full" else "translation-test")
    assert Path(result["launcher_path"]).parent == Path(item.playable_root).parent
    assert "default_launcher_path" not in result
    assert not (Path(item.playable_root).parent / "启动汉化版.cmd").exists()
    assert executable == Path(item.playable_root) / "game/M.C.催眠研究.exe"
    assert manifest["profile"] == f"yuris479-detected-{mode}-v2"
    assert manifest["translated_count"] == expected_count and manifest["unchanged_ordinals"] == []
    assert manifest["unselected_count"] == 76 - expected_count
    assert manifest["locale_settings_path"] == str(settings.path)
    assert not any("locale-fix" in name for name in manifest["files"])
    assert Path(item.playable_root) in executable.parents
    assert (Path(item.playable_root) / "game/movies/opening.dat").read_bytes() == b"runtime movie"
    assert (Path(item.playable_root) / "game/pac/bgm.ypf").read_bytes() == b"title-specific resource"
    assert not (Path(item.playable_root) / "game/save").exists()
    assert not list(Path(item.output_root).rglob("*.ypf"))
    assert original_files == {
        path.relative_to(game): path.read_bytes()
        for path in game.rglob("*")
        if path.is_file()
    }
    receipt = Path(item.output_root) / "workflow.json"
    yuris.save_json(receipt, item.public_dict())
    restored = TranslationWorkflows(webapp).restore(receipt)
    assert restored.launcher_path == item.launcher_path
    assert restored.playable_layout_version == 2
    assert restored.playable_root == item.playable_root
    # Older receipts still load after their redundant root launcher is removed.
    legacy_state = item.public_dict()
    legacy_state["default_launcher_path"] = str(Path(item.playable_root).parent / "启动汉化版.cmd")
    yuris.save_json(receipt, legacy_state)
    legacy_restored = TranslationWorkflows(webapp).restore(receipt)
    assert legacy_restored.launcher_path == item.launcher_path
    assert "default_launcher_path" not in legacy_restored.public_dict()
    assert len(requests) == expected_count  # Restoring never calls the model.
    manifest_path = Path(item.playable_root) / "deployment.json"
    changed = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed["translated_count"] += 1
    yuris.save_json(manifest_path, changed)
    with pytest.raises(ValueError, match="已更新为其他版本"):
        TranslationWorkflows(webapp).restore(receipt)
