import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_translation_workflow import settle

from nagi.gameio import deployment as deploy
from nagi.gameio.deployment_runtime.launch import verified_manifest
from nagi.translation_workflow import TranslationWorkflows
from nagi.webapp import TranslationJob


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    game, trial, storage = (tmp_path / name for name in ("game", "trial", "outputs"))
    for folder in (game, trial, storage):
        folder.mkdir()

    def fill(root, names):
        checksums = {}
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("fixture-" + name).encode())
            checksums[name] = deploy.digest(path)
        return checksums

    monkeypatch.setattr(deploy, "GAME_FILES", fill(game, deploy.GAME_FILES))
    monkeypatch.setattr(deploy, "TRIAL_FILES", fill(trial, deploy.TRIAL_FILES))
    monkeypatch.setattr(
        deploy, "LOCALE_FILES", fill(trial / "game/locale-fix/LE", deploy.LOCALE_FILES)
    )
    monkeypatch.setattr(deploy, "trial_root", lambda: trial)
    monkeypatch.setattr(deploy.importlib.util, "find_spec", lambda name: object())
    backend = SimpleNamespace(TranslationJob=TranslationJob)
    service = TranslationWorkflows(backend, deployer=deploy.deploy_workflow)
    item = service.create(str(game), str(storage), reuse_trial=True)
    assert settle(item)["stages"]["translate"]["status"] == "completed"
    return service, item, game, trial


def test_restores_verified_results_without_model_configuration(prepared):
    service, item, _game, _trial = prepared
    payload = item.public_dict()
    assert payload["source_kind"] == deploy.PROFILE_ID
    assert payload["job"]["translated_units"] == 29
    assert Path(payload["corpus_dir"]).is_dir()
    with pytest.raises(ValueError, match="不支持"):
        service.start(item.workflow_id, "translate", mode="full", confirmed=True)


def test_real_deployer_creates_isolated_package_and_launcher(prepared):
    service, item, game, _trial = prepared
    (game / "save").mkdir()
    (game / "save/original.sav").write_text("keep this save")
    before = {
        str(p.relative_to(game)): p.read_bytes() for p in game.rglob("*") if p.is_file()
    }
    service.start(item.workflow_id, "deploy")
    payload = settle(item)
    assert payload["stages"]["deploy"]["status"] == "completed", payload
    launcher = Path(payload["launcher_path"])
    assert launcher.suffix == ".cmd" and launcher.is_file()
    internal = Path(payload["deployment_launcher_path"])
    assert "locate_runtime.ps1" in internal.read_text(encoding="utf-8")
    assert "python.exe" not in internal.read_text(encoding="utf-8")
    root = internal.parent
    assert root.name == "translation-test"
    assert root.parent == launcher.parent
    assert not list(root.glob("localized-game-*"))
    assert not (root / "game/save").exists()
    assert not list(Path(item.output_root).glob("*.partial"))
    assert (root / "game/pac/ysbin.ypf").read_bytes() != (
        game / "pac/ysbin.ypf"
    ).read_bytes()
    manifest, executable = verified_manifest(root)
    assert executable == root / "game/M.C.2催眠研究.exe"
    assert manifest["unchanged_ordinals"] == [10, 19, 22]
    assert before == {
        str(p.relative_to(game)): p.read_bytes() for p in game.rglob("*") if p.is_file()
    }
    service.start(item.workflow_id, "deploy")
    assert item.launcher_path == str(launcher)


@pytest.mark.parametrize("file_kind", ["game", "translation", "locale"])
def test_changed_inputs_fail_before_game_copy(prepared, file_kind):
    service, item, game, _trial = prepared
    snapshot = Path(item.translation_job.translated_scripts_dir)
    path = {
        "game": game / "pac/ysbin.ypf",
        "translation": snapshot / "translations.json",
        "locale": snapshot / "locale/LEProc.exe",
    }[file_kind]
    path.write_text("tampered")
    service.start(item.workflow_id, "deploy")
    payload = settle(item)
    assert payload["stages"]["deploy"]["status"] == "failed"
    assert payload["launcher_path"] is None
    assert not list(Path(item.output_root).glob("localized-game-*"))
    assert not list(Path(item.output_root).glob("*.partial"))


def test_partial_copy_failure_never_publishes_and_retry_uses_new_folder(
    prepared, monkeypatch
):
    service, item, _game, _trial = prepared
    original_copy = deploy.copy_checked

    def fail(source, target, checksum, *args):
        original_copy(source, target, checksum, *args)
        raise OSError("simulated disk failure")

    monkeypatch.setattr(deploy, "copy_checked", fail)
    service.start(item.workflow_id, "deploy")
    assert settle(item)["stages"]["deploy"]["status"] == "failed"
    assert item.launcher_path is None
    partial = list(Path(item.playable_root).parent.glob("*.partial"))
    assert len(partial) == 1 and (partial[0] / "FAILED.txt").is_file()
    monkeypatch.setattr(deploy, "copy_checked", original_copy)
    service.start(item.workflow_id, "deploy")
    assert settle(item)["stages"]["deploy"]["status"] == "completed"
    assert partial[0].is_dir()


def test_disk_space_and_missing_dependency_fail_closed(prepared, monkeypatch):
    service, item, _game, _trial = prepared
    monkeypatch.setattr(
        deploy.shutil, "disk_usage", lambda path: SimpleNamespace(free=0)
    )
    service.start(item.workflow_id, "deploy")
    assert "空间" in settle(item)["stages"]["deploy"]["error"]
    monkeypatch.setattr(deploy.importlib.util, "find_spec", lambda name: None)
    service.start(item.workflow_id, "deploy")
    assert "Frida" in settle(item)["stages"]["deploy"]["error"]


def test_launcher_rejects_tampered_and_escaping_manifest(prepared):
    service, item, _game, _trial = prepared
    service.start(item.workflow_id, "deploy")
    settle(item)
    root = Path(item.deployment_launcher_path).parent
    manifest_path = root / "deployment.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["../outside"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsafe"):
        verified_manifest(root)
