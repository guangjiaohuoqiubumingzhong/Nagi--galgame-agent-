import json
import os
import subprocess
from pathlib import Path

import pytest

from nagi.game_launcher import publish_launcher, validate_version_launcher
from nagi.translation_storage import playable_root, publish_playable, workflow_root


@pytest.mark.parametrize("engine", ["QLIE", "YU-RIS"])
def test_modes_share_fixed_slots_across_tasks_and_versions(tmp_path, engine):
    storage, game = tmp_path / "translations", tmp_path / "original/Game"
    for task in ("first", "second"):
        source = workflow_root(storage, game, engine, task)
        formal = playable_root(storage, source, mode="full")
        trial = playable_root(storage, source, mode="partial")
        assert formal.name == "translation-formal" and trial.name == "translation-test"
        assert formal.parent == trial.parent == tmp_path / "playable" / engine / source.parent.name
        assert playable_root(storage, source, mode="pilot") == trial


def test_republish_archives_entire_old_copy_including_saves(tmp_path):
    root = tmp_path / "playable/FutureEngine/Game"
    root.mkdir(parents=True)
    slot, staging = root / "translation-formal", root / ".deploy-new.partial"
    slot.mkdir()
    (slot / "deployment.json").write_text(json.dumps({"schema_version": 1}))
    (slot / "save.dat").write_bytes(b"valuable save")
    staging.mkdir()
    (staging / "game.exe").write_bytes(b"new game, never executed")
    backup = publish_playable(staging, slot)
    assert slot.name == "translation-formal" and not staging.exists()
    assert (slot / "game.exe").read_bytes() == b"new game, never executed"
    assert (backup / "save.dat").read_bytes() == b"valuable save"
    assert tmp_path / "playable/.history/FutureEngine/Game" == backup.parent


def test_publish_failure_restores_old_slot(tmp_path, monkeypatch):
    root = tmp_path / "playable/QLIE/Game"
    root.mkdir(parents=True)
    slot, staging = root / "translation-test", root / ".deploy-new.partial"
    slot.mkdir()
    (slot / "deployment.json").write_text(json.dumps({"schema_version": 1}))
    staging.mkdir()
    original = Path.rename

    def fail_new(path, target):
        if path == staging:
            raise OSError("fixture publication failure")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", fail_new)
    with pytest.raises(OSError, match="publication"):
        publish_playable(staging, slot)
    assert slot.is_dir() and (slot / "deployment.json").is_file()
    assert staging.is_dir()


def test_unmanaged_output_is_not_moved(tmp_path):
    slot, staging = tmp_path / "translation-test", tmp_path / ".deploy-new.partial"
    slot.mkdir()
    staging.mkdir()
    (slot / "personal.txt").write_text("keep")
    with pytest.raises(ValueError, match="不会移动"):
        publish_playable(staging, slot)
    assert (slot / "personal.txt").read_text() == "keep"


@pytest.mark.parametrize("engine", ["QLIE", "YU-RIS", "FutureEngine"])
def test_root_launchers_work_for_any_engine_and_do_not_move_dependencies(tmp_path, engine):
    root = tmp_path / engine / "中文 Game & demo!"
    internal = root / "translation-formal/启动汉化版.cmd"
    internal.parent.mkdir(parents=True)
    internal.write_bytes(b'@echo off\r\necho WRAPPER_TEST_OK\r\necho "%CD%"\r\nexit /b 7\r\n')
    original = internal.read_bytes()
    version = publish_launcher(root, internal, "full")
    assert not (root / "启动汉化版.cmd").exists()
    assert version == root / "启动正式版.cmd"
    assert "translation-formal" in version.read_text(encoding="utf-8")
    assert str(tmp_path) not in version.read_text(encoding="utf-8")
    assert internal.read_bytes() == original
    assert validate_version_launcher(root, internal, "full", version) == version
    if os.name == "nt":
        command = f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /s /c ""{version}""'
        run = subprocess.run(command, check=False,
                             cwd=tmp_path, capture_output=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        assert run.returncode == 7, run.stdout + run.stderr
        assert b"WRAPPER_TEST_OK" in run.stdout
        assert str(internal.parent).encode("utf-8") in run.stdout
    version.write_text("user-owned custom launcher", encoding="utf-8")
    with pytest.raises(ValueError, match="不会覆盖"):
        publish_launcher(root, internal, "full")


@pytest.mark.parametrize("test_mode", ["partial", "pilot"])
def test_test_and_formal_entries_stay_independent(tmp_path, test_mode):
    root = tmp_path / "playable/FutureEngine/Game"
    formal = root / "translation-formal/启动汉化版.cmd"
    trial = root / "translation-test/启动汉化版.cmd"
    for target in (formal, trial):
        target.parent.mkdir(parents=True)
        target.write_bytes(b"@echo off\r\nexit /b 0\r\n")
    formal_entry = publish_launcher(root, formal, "full")
    original = formal_entry.read_bytes()
    test_entry = publish_launcher(root, trial, test_mode)
    assert test_entry == root / "启动测试版.cmd"
    assert formal_entry.read_bytes() == original
    assert validate_version_launcher(root, trial, test_mode, test_entry) == test_entry
    assert {p.name for p in root.glob("*.cmd")} == {"启动测试版.cmd", "启动正式版.cmd"}
    # Rebuilding either mode cannot recreate the old generic entry.
    publish_launcher(root, formal, "full")
    assert not (root / "启动汉化版.cmd").exists()
