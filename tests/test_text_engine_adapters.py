import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from test_translation_workflow import settle

from nagi.gameio.deployment import deploy_workflow
from nagi.gameio.text_engines import XP3_SIGNATURE, Xp3Archive, detect_engine
from nagi.translation_workflow import TranslationWorkflows
from nagi.webapp import TranslationJob


class EchoChineseClient:
    last_completion_metadata: ClassVar = {"fixture": True}

    def complete(self, messages, max_new_tokens):
        assert max_new_tokens == 8192
        payload = json.loads(messages[-1]["content"])
        return json.dumps(
            {"translations": [
                {"id": row["id"], "text": "中" + row["text"]}
                for row in payload["texts"]
            ]},
            ensure_ascii=False,
        )


class BrokenTokenClient(EchoChineseClient):
    def complete(self, messages, max_new_tokens):
        response = json.loads(super().complete(messages, max_new_tokens))
        for row in response["translations"]:
            row["text"] = row["text"].replace("[p]", "")
        return json.dumps(response, ensure_ascii=False)


def backend(client=EchoChineseClient):
    return SimpleNamespace(
        TranslationJob=TranslationJob,
        _configured_model=lambda: {
            "provider": "fixture", "model": "fixture-model", "api_key": "fixture-key"
        },
        _model_client=lambda _config: client(),
        assess_game_directory=lambda _path: {
            "supported_archive_count": 0, "key_available": False
        },
    )


def _fixture_game(root, engine):
    game = root / engine
    game.mkdir()
    executable = game / f"{engine}.exe"
    executable.write_bytes(b"MZ fixture " + (b"TVP(KIRIKIRI)" if engine == "kirikiri" else b""))
    if engine == "kirikiri":
        script = game / "scenario.ks"
        script.write_bytes(";comment\r\nこんにちは[p]\r\n@wait time=1\r\nさようなら\r\n".encode("cp932"))
    elif engine == "renpy":
        (game / "renpy").mkdir()
        (game / "game").mkdir()
        script = game / "game/script.rpy"
        script.write_text(
            'label start:\n    e "こんにちは"\n    "さようなら"\n    jump end\n',
            encoding="utf-8",
        )
    else:
        (game / "tyrano").mkdir()
        (game / "tyrano/tyrano.base.js").write_text("fixture", encoding="utf-8")
        (game / "data/scenario").mkdir(parents=True)
        script = game / "data/scenario/first.ks"
        script.write_text(";comment\nこんにちは[p]\n@wait time=1\nさようなら\n", encoding="utf-8")
    return game, script


@pytest.mark.parametrize(
    ("engine", "family"),
    [("kirikiri", "KiriKiri"), ("renpy", "RenPy"), ("tyranoscript", "TyranoScript")],
)
def test_text_engines_complete_existing_three_stage_workflow(tmp_path, engine, family):
    game, source_script = _fixture_game(tmp_path, engine)
    storage = tmp_path / "translations"
    storage.mkdir()
    original = source_script.read_bytes()
    service = TranslationWorkflows(
        backend(), deployer=deploy_workflow, state_path=tmp_path / "latest-workflow.json"
    )

    item = service.create(str(game), str(storage))
    extracted = settle(item)
    assert extracted["engine_family"] == family
    assert extracted["source_kind"] == engine
    assert extracted["stages"]["extract"]["status"] == "completed"
    assert extracted["job"]["total_units"] == 2

    service.start(item.workflow_id, "translate", mode="full", confirmed=True)
    translated = settle(item)
    assert translated["stages"]["translate"]["status"] == "completed", translated
    assert translated["job"]["translated_units"] == 2

    service.start(item.workflow_id, "deploy")
    deployed = settle(item)
    assert deployed["stages"]["deploy"]["status"] == "completed", deployed
    assert source_script.read_bytes() == original
    internal = Path(deployed["deployment_launcher_path"])
    assert internal.is_file() and internal.parent.name == "translation-formal"
    localized = internal.parent / "game" / source_script.relative_to(game)
    localized_bytes = localized.read_bytes()
    localized_text = localized_bytes.decode("utf-16") if engine == "kirikiri" else localized_bytes.decode("utf-8-sig")
    assert "中こんにちは" in localized_text and "中さようなら" in localized_text
    if engine != "renpy":
        assert "[p]" in localized_text
    assert Path(deployed["launcher_path"]).name == "启动正式版.cmd"
    receipt = Path(item.output_root) / "workflow.json"
    restored = TranslationWorkflows(backend(), deployer=deploy_workflow).restore(receipt)
    assert restored.source_kind == engine
    assert restored.launcher_path == deployed["launcher_path"]


def test_text_engine_repair_merges_only_untranslated_rows_and_stays_full(tmp_path):
    game, _source_script = _fixture_game(tmp_path, "kirikiri")
    storage = tmp_path / "translations"
    storage.mkdir()
    submitted = []

    class Client:
        repair = False
        last_completion_metadata: ClassVar = {"fixture": True}

        def complete(self, messages, max_new_tokens):
            rows = json.loads(messages[-1]["content"])["texts"]
            submitted.append((self.repair, [row["id"] for row in rows]))
            if self.repair:
                values = ["补翻中文[p]" for _row in rows]
            else:
                values = [rows[0]["text"], "已有中文"]
            return json.dumps({"translations": [
                {"id": row["id"], "text": value}
                for row, value in zip(rows, values)
            ]}, ensure_ascii=False)

    client = Client()
    service = TranslationWorkflows(
        backend(lambda: client), deployer=deploy_workflow,
        state_path=tmp_path / "latest-workflow.json",
    )
    item = service.create(str(game), str(storage))
    settle(item)
    service.start(item.workflow_id, "translate", mode="full", confirmed=True)
    assert settle(item)["stages"]["translate"]["status"] == "completed"
    full_output = item.translation_job.output_root
    client.repair = True
    service.start(item.workflow_id, "translate", mode="repair", confirmed=True)
    repaired = settle(item)
    assert repaired["stages"]["translate"]["status"] == "completed", repaired
    assert repaired["translation_action"] == "repair"
    assert repaired["mode"] == "full"
    assert item.translation_job.output_root == full_output
    assert len(submitted) == 2 and len(submitted[-1][1]) == 1
    translations = json.loads(
        (Path(full_output) / "translations.json").read_text(encoding="utf-8")
    )
    assert set(translations.values()) == {"补翻中文[p]", "已有中文"}
    service.start(item.workflow_id, "deploy")
    deployed = settle(item)
    assert deployed["stages"]["deploy"]["status"] == "completed", deployed
    assert Path(deployed["launcher_path"]).name == "启动正式版.cmd"
    receipt = Path(item.output_root) / "workflow.json"
    restored = TranslationWorkflows(
        backend(lambda: client), deployer=deploy_workflow
    ).restore(receipt)
    assert restored.translation_action == "repair"
    assert restored.translation_job.mode == "full"


def _xp3(path, files):
    records = []
    with path.open("wb") as output:
        output.write(XP3_SIGNATURE + b"\0" * 8)
        for name, data in files.items():
            offset = output.tell()
            output.write(data)
            info = struct.pack("<IQQH", 0, len(data), len(data), len(name)) + name.encode("utf-16le")
            segm = struct.pack("<IQQQ", 0, offset, len(data), len(data))
            chunks = b"info" + struct.pack("<Q", len(info)) + info
            chunks += b"segm" + struct.pack("<Q", len(segm)) + segm
            checksum = struct.pack("<I", zlib.adler32(data) & 0xFFFFFFFF)
            chunks += b"adlr" + struct.pack("<Q", 4) + checksum
            records.append(b"File" + struct.pack("<Q", len(chunks)) + chunks)
        index_offset = output.tell()
        index = b"".join(records)
        output.write(b"\0" + struct.pack("<QQ", len(index), len(index)) + index)
        output.seek(len(XP3_SIGNATURE))
        output.write(struct.pack("<Q", index_offset))


def test_standard_xp3_is_read_and_rebuilt_without_changing_other_assets(tmp_path):
    source = tmp_path / "data.xp3"
    _xp3(source, {"scenario/first.ks": "こんにちは[p]\n".encode("cp932"), "image/a.bin": b"asset"})
    archive = Xp3Archive(source)
    entries = {entry.name: entry for entry in archive.entries}
    assert archive.read(entries["image/a.bin"]) == b"asset"
    target = tmp_path / "patched.xp3"
    replacement = "中文[p]\n".encode("utf-16")
    archive.rewrite(target, {"scenario/first.ks": replacement})
    rebuilt = Xp3Archive(target)
    entries = {entry.name: entry for entry in rebuilt.entries}
    assert rebuilt.read(entries["scenario/first.ks"]) == replacement
    assert rebuilt.read(entries["image/a.bin"]) == b"asset"


def test_renpy_compiled_only_release_fails_closed(tmp_path):
    game = tmp_path / "compiled"
    (game / "game").mkdir(parents=True)
    (game / "renpy").mkdir()
    (game / "compiled.exe").write_bytes(b"MZ")
    (game / "game/script.rpyc").write_bytes(b"compiled")
    assert detect_engine(game) == "renpy"
    storage = tmp_path / "translations"
    storage.mkdir()
    item = TranslationWorkflows(backend()).create(str(game), str(storage))
    payload = settle(item)
    assert payload["stages"]["extract"]["status"] == "failed"
    assert "rpyc" in payload["stages"]["extract"]["error"]


def test_changed_engine_control_tokens_are_never_deployed(tmp_path):
    game, source = _fixture_game(tmp_path, "tyranoscript")
    original = source.read_bytes()
    storage = tmp_path / "translations"
    storage.mkdir()
    service = TranslationWorkflows(backend(BrokenTokenClient), deployer=deploy_workflow)
    item = service.create(str(game), str(storage))
    settle(item)
    service.start(item.workflow_id, "translate", mode="full", confirmed=True)
    assert settle(item)["stages"]["translate"]["status"] == "completed"
    service.start(item.workflow_id, "deploy")
    payload = settle(item)
    assert payload["stages"]["deploy"]["status"] == "failed"
    assert "控制标签" in payload["stages"]["deploy"]["error"]
    assert source.read_bytes() == original
    assert payload["launcher_path"] is None
