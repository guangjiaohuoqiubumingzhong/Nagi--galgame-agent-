import json
from pathlib import Path

import pytest

from nagi.translation_storage import (
    engine_family,
    game_folder,
    playable_root,
    workflow_root,
)


def test_versions_share_one_engine_folder_but_installations_do_not(tmp_path):
    assert engine_family("yuris-479") == engine_family("yuris-500") == "YU-RIS"
    assert engine_family("qlie-3.0") == engine_family("qlie-3.1") == "QLIE"
    assert engine_family("kirikiri-z") == "KiriKiri"
    assert engine_family("renpy-8") == "RenPy"
    assert engine_family("tyranoscript-v5") == "TyranoScript"
    first, second = tmp_path / "a/Game", tmp_path / "b/Game"
    assert game_folder(first) != game_folder(second)
    root = workflow_root(tmp_path / "translations", first, "yuris-479", "abc")
    assert root.parent.parent.name == "YU-RIS"
    assert playable_root(
        tmp_path / "translations", root
    ) == tmp_path / "playable" / root.relative_to(tmp_path / "translations")
    with pytest.raises(ValueError):
        workflow_root(tmp_path, first, "unknown", "abc")
    with pytest.raises(ValueError):
        workflow_root(tmp_path, first, "QLIE", "../outside")


def test_moved_workflow_restores_external_playable_without_model_calls(
    tmp_path, monkeypatch
):
    from test_translation_workflow import settle
    from test_yuris import synthetic_archive

    from nagi import webapp
    from nagi.translation_workflow import TranslationWorkflows

    game, storage = tmp_path / "game", tmp_path / "translations"
    (game / "pac").mkdir(parents=True)
    (game / "game.exe").write_bytes(b"MZ synthetic YU-RIS executable")
    storage.mkdir()
    (game / "pac/ysbin.ypf").write_bytes(synthetic_archive())
    service = TranslationWorkflows(webapp, state_path=tmp_path / "latest.json")
    item = service.create(str(game), str(storage))
    settle(item)
    receipt = Path(item.output_root) / "workflow.json"
    state = json.loads(receipt.read_text(encoding="utf-8"))
    launcher = Path(item.playable_root) / "localized/启动汉化版.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("test only", encoding="utf-8")
    state["launcher_path"] = str(launcher)
    receipt.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        webapp, "_configured_model", lambda: pytest.fail("No API configuration needed")
    )
    restored = TranslationWorkflows(webapp).restore(receipt)
    assert restored.launcher_path == str(launcher)
    state["launcher_path"] = str(tmp_path / "outside.cmd")
    receipt.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="Launcher escapes"):
        TranslationWorkflows(webapp).restore(receipt)
