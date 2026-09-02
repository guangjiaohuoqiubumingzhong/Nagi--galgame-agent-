import json
import struct
import threading
import urllib.request
from pathlib import Path

import pytest
from test_qlie_payload import write_payload_pack30, write_synthetic_pe

from nagi.providers import OpenAIChatCompatibleModelClient
from nagi.webapp import (
    QlieWebServer,
    TranslationJob,
    _model_client,
    assess_game_directory,
)


def write_synthetic_pack(path):
    signature = b"FilePackVer3.1\x00"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"payload" + b"toc" + struct.pack("<16sIII", signature, 1, 7, 0))


def test_assessment_is_read_only_and_detects_patch_pack_candidate(
    tmp_path, monkeypatch
):
    game = tmp_path / "game"
    write_synthetic_pack(game / "GameData" / "data0.pack")
    write_synthetic_pack(game / "GameData" / "data10.pack")
    write_synthetic_pe(game / "game.exe", bytes(range(256)))
    (game / "version.txt").write_bytes("version=1\r\npatch0=\r\n".encode("utf-16-le"))
    before = {path: path.read_bytes() for path in game.rglob("*") if path.is_file()}
    monkeypatch.setattr("nagi.webapp._prepared_workspace", lambda _path: None)

    result = assess_game_directory(game)

    assert result["status"] == "supported"
    assert result["supported_archive_count"] == 2
    assert result["key_available"] is True
    assert result["pack_assessment"]["patch_pack_candidate"] is True
    assert result["pack_assessment"]["highest_data_pack"] == 10
    assert result["pack_assessment"]["version_patch_slots"] == 1
    assert before == {
        path: path.read_bytes() for path in game.rglob("*") if path.is_file()
    }


def test_assessment_probes_multiple_filepack30_exes_for_the_matching_key(
    tmp_path, monkeypatch
):
    game = tmp_path / "game"
    game_data = game / "GameData"
    dll_dir = game / "DLL"
    game_data.mkdir(parents=True)
    dll_dir.mkdir()
    external_key = bytes((index * 3) & 0xFF for index in range(4096))
    internal_key = bytes((index * 5 + 1) & 0xFF for index in range(4096))
    correct_game_key = bytes((index * 7 + 2) & 0xFF for index in range(256))
    wrong_game_key = bytes(reversed(correct_game_key))
    (dll_dir / "key.fkey").write_bytes(external_key)
    write_payload_pack30(
        game_data / "data0.pack",
        external_key,
        internal_key,
        correct_game_key,
        b"message('synthetic key probe')\r\n",
    )
    wrong_exe = game / "larger-tool.exe"
    right_exe = game / "game.exe"
    write_synthetic_pe(wrong_exe, bytes(range(256)), icon_key=wrong_game_key)
    wrong_exe.write_bytes(wrong_exe.read_bytes() + bytes(1024))
    write_synthetic_pe(right_exe, bytes(range(256)), icon_key=correct_game_key)
    monkeypatch.setattr("nagi.webapp._prepared_workspace", lambda _path: None)

    result = assess_game_directory(game)

    assert Path(result["exe_path"]) == right_exe.resolve()
    assert result["key_available"] is True


def test_job_public_payload_omits_threading_state_and_secrets(tmp_path):
    job = TranslationJob("abc", str(tmp_path), "pilot", str(tmp_path / "out"))
    job.update(event="started", status="running")

    payload = job.public_dict()

    assert payload["status"] == "running"
    assert payload["events"][0]["message"] == "started"
    assert "lock" not in payload
    assert "cancel_event" not in payload
    assert "api_key" not in json.dumps(payload)


def test_local_server_config_never_returns_api_key(monkeypatch):
    monkeypatch.setattr(
        "nagi.webapp._configured_model",
        lambda: {
            "provider": "deepseek",
            "model": "fixture-model",
            "base_url": "https://example.invalid",
            "api_key_configured": True,
            "api_key": "fixture-secret",
        },
    )
    server = QlieWebServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/api/config"
        ) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body)
        assert payload["model"] == "fixture-model"
        assert payload["api_key_configured"] is True
        assert "fixture-secret" not in body
        assert 'api_key"' not in body
        assert response.headers["X-Frame-Options"] == "DENY"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_translation_uses_deepseek_json_mode(monkeypatch):
    monkeypatch.setattr(
        "nagi.webapp._configured_model",
        lambda: {
            "provider": "deepseek",
            "model": "deepseek-v4-flash-vision-exp",
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key_configured": True,
            "api_key": "fixture-secret",
        },
    )

    client = _model_client()

    assert isinstance(client, OpenAIChatCompatibleModelClient)
    assert client.base_url == "https://api.deepseek.com"
    assert client.response_format == {"type": "json_object"}
    assert client.thinking == {"type": "disabled"}


def test_job_http_api_rejects_removed_reference_import(tmp_path, monkeypatch):
    from nagi import webapp
    received = []

    def start(game_dir, mode):
        received.append(game_dir)
        return TranslationJob("fixture", game_dir, mode, str(tmp_path / "output"))

    monkeypatch.setattr(webapp, "start_translation_job", start)
    server = QlieWebServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        context = {"terminology": [{"source": "term", "target": "target"}]}
        payload = {"csrf_token": server.csrf_token, "confirmed": True,
                   "game_dir": str(tmp_path), "mode": "pilot", "context": context}
        url = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(url + "/api/jobs", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": url})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 400
        assert not received
        del payload["context"]
        request = urllib.request.Request(url + "/api/jobs", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": url})
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
        assert received == [str(tmp_path)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("purpose", ["game", "storage", "locale", "workspace"])
def test_directory_picker_http_selection_cancel_failure_and_recovery(tmp_path, monkeypatch, purpose):
    import subprocess

    from nagi import directory_picker, webapp

    monkeypatch.setattr(webapp, "_project_root", lambda: tmp_path)
    server = QlieWebServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    route = "/api/agent/select-workspace" if purpose == "workspace" else "/api/select-directory"
    added = []

    def add_workspace(path):
        added.append(path)
        return {"path": path}

    monkeypatch.setattr(server.agent_service, "add_workspace", add_workspace)

    def post(token=server.csrf_token):
        return urllib.request.urlopen(urllib.request.Request(
            base + route,
            data=json.dumps({"purpose": purpose, "csrf_token": token}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
        ), timeout=5)

    def failure(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "picker", stderr="missing GUI component")

    monkeypatch.setattr(directory_picker, "_run_dialog", failure)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            post("")
        assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as error:
            post()
        assert error.value.code == 400
        assert "手动填写" in json.load(error.value)["error"]
        assert added == []
        for selected in (None, str(tmp_path / "中文 游戏 & 目录")):
            monkeypatch.setattr(directory_picker, "_run_dialog", lambda command, windows, selected=selected:
                                subprocess.CompletedProcess(command, 0, json.dumps({"path": selected})))
            with post() as response:
                payload = json.load(response)
            if purpose == "workspace":
                assert payload == {"workspace": {"path": selected} if selected else None}
                assert added == ([selected] if selected else [])
            else:
                assert payload == {"path": selected}
        with urllib.request.urlopen(base + "/api/config") as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
