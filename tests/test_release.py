"""Release boundaries: local identity, offline startup, compatibility and safety."""
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from urllib import error, request

import pytest

from nagi import __version__, paths, webapp
from nagi.config import load_project_env
from nagi.game_launcher import repair_runtime, write_runtime_launcher
from nagi.gameio.deployment_runtime.launch import verified_manifest
from nagi.launcher import probe
from scripts.build_release import source_files


def test_resource_location_does_not_follow_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert (paths.resource_root() / "web_ui/index.html").is_file()
    assert (paths.resource_root() / "gameio/deployment_runtime/locate_runtime.ps1").is_file()


def test_isolated_data_directory_does_not_import_parent_credentials(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("NAGI_TEST_PARENT_SECRET=fixture\n")
    child = tmp_path / "empty-data"
    child.mkdir()
    monkeypatch.delenv("NAGI_TEST_PARENT_SECRET", raising=False)
    assert load_project_env(child, search_parents=False) == {}


def test_legacy_state_is_preserved_and_future_versions_rejected(tmp_path):
    legacy = tmp_path / ".pico"
    legacy.mkdir()
    payload = b'{"credential":"dpapi:not-a-real-key"}'
    (legacy / "private.json").write_bytes(payload)
    assert paths.ensure_data_directory() == tmp_path
    assert paths.state_root() == legacy
    assert (legacy / "private.json").read_bytes() == payload
    marker = legacy / "data-version.json"
    marker.write_text('{"schema_version":999}', encoding="utf-8")
    with pytest.raises(ValueError, match="数据版本"):
        paths.ensure_data_directory()
    assert json.loads(marker.read_text())["schema_version"] == 999


def test_source_manifest_rejects_private_or_missing_input(tmp_path):
    (tmp_path / "release").mkdir()
    manifest = tmp_path / "release/source-files.txt"
    (tmp_path / ".env").write_text("never include")
    for name in (".env", "../outside", ".pico/private.json", "absent.py"):
        manifest.write_text(name)
        with pytest.raises(ValueError):
            list(source_files(tmp_path))


@pytest.fixture
def local_server(monkeypatch):
    monkeypatch.setattr(webapp, "JOBS", webapp.JobRegistry())
    monkeypatch.setattr(webapp, "_model_client", lambda *args: pytest.fail("Unexpected paid request"))
    server = webapp.QlieWebServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(2)


def fetch(server, path, body=None, headers=None):
    address = f"http://127.0.0.1:{server.server_address[1]}"
    return request.urlopen(request.Request(address + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})}), timeout=3)


def test_first_run_is_offline_and_assets_are_packaged(local_server):
    server = local_server
    assert probe(server.server_address[1])["version"] == __version__
    with fetch(server, "/api/config") as response:
        config = json.load(response)
    assert config["release"]["first_run"]
    assert config["release"]["features"]["bm25"]
    for name in ("/", "/app.js", "/release.js", "/styles.css", "/nagi-avatar.png", "/nagi-avatar.svg"):
        with fetch(server, name) as response:
            assert response.status == 200 and response.read()
    with pytest.raises(error.HTTPError) as failure:
        fetch(server, "/api/settings/onboarding", {"acknowledged": True})
    assert failure.value.code == 403
    with fetch(server, "/api/settings/onboarding", {"acknowledged": True, "csrf_token": config["csrf_token"]}):
        pass
    with fetch(server, "/api/config") as response:
        assert not json.load(response)["release"]["first_run"]


def test_service_rejects_foreign_host_and_cross_origin(local_server):
    for headers in ({"Host":"attacker.example"}, {"Origin":"https://attacker.example"}):
        with pytest.raises(error.HTTPError) as failure:
            fetch(local_server, "/api/config", headers=headers)
        assert failure.value.code == 403


def test_busy_service_cannot_be_shutdown(local_server):
    server = local_server
    server.agent_service.jobs["synthetic"] = SimpleNamespace(status="running")
    with pytest.raises(error.HTTPError) as failure:
        fetch(server, "/api/shutdown", {"csrf_token": server.csrf_token})
    assert failure.value.code == 400
    server.agent_service.jobs.clear()
    with fetch(server, "/api/shutdown", {"csrf_token": server.csrf_token}) as response:
        assert json.load(response)["ok"]


def test_foreign_service_is_not_reused(monkeypatch):
    monkeypatch.setattr("nagi.launcher.request", lambda *args: {"app_id":"other-service"})
    with pytest.raises(ValueError, match="其他程序"):
        probe(8765)


def test_game_runtime_repair_preserves_game_and_saves(tmp_path, monkeypatch):
    app = tmp_path / "new-nagi"
    app.mkdir()
    monkeypatch.setenv("NAGI_APP_ROOT", str(app))
    root = tmp_path / "playable/translation-test"
    (root / "runtime").mkdir(parents=True)
    (root / "game").mkdir()
    files = {"game/demo.exe":b"synthetic executable", "runtime/launch.py":b"# old launcher"}
    for name, value in files.items():
        (root / name).write_bytes(value)
    (root / "game/save.dat").write_bytes(b"preserve save")
    manifest = {"schema_version":1, "executable":"game/demo.exe", "files": {name:hashlib.sha256(value).hexdigest() for name,value in files.items()}}
    (root / "deployment.json").write_text(json.dumps(manifest))
    (root / "启动汉化版.cmd").write_text('"C:\\old-python\\python.exe" launch.py')
    backup = repair_runtime(root)
    assert (backup / "runtime/launch.py").read_bytes() == files["runtime/launch.py"]
    assert (root / "game/demo.exe").read_bytes() == files["game/demo.exe"]
    assert (root / "game/save.dat").read_bytes() == b"preserve save"
    assert "python.exe" not in (root / "启动汉化版.cmd").read_text(encoding="utf-8")
    assert (root / "重新定位Nagi.cmd").is_file()
    verified_manifest(root)


def test_generated_launcher_uses_relative_app_without_python_path(tmp_path):
    root = tmp_path / "copy"
    root.mkdir()
    write_runtime_launcher(root)
    locator = json.loads((root / "nagi-runtime.json").read_text())
    assert locator["relative_app"] == ".."
    assert str(Path(__file__).anchor) not in (root / "启动汉化版.cmd").read_text(encoding="utf-8")
