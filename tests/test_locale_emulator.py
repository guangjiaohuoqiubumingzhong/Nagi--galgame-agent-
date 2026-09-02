import json
import struct
import threading
import xml.etree.ElementTree as ET
from urllib import error, request

import pytest

from nagi.gameio.deployment_runtime import locale_support
from nagi.gameio.deployment_runtime.launch import LEGACY_LOCALE_FILES, locale_command
from nagi.locale_emulator import LocaleEmulatorSettings


def make_locale(root):
    root.mkdir(parents=True)
    for name in locale_support.BINARIES:
        content = bytearray(128)
        content[:2] = b"MZ"
        struct.pack_into("<I", content, 60, 64)
        content[64:68] = b"PE\0\0"
        (root / name).write_bytes(content)
    (root / "LEVersion.xml").write_text(
        '<LEVersion Version="2.5.0.1" />', encoding="utf-8"
    )
    (root / "LEConfig.xml").write_text(
        "user's unrelated global settings", encoding="utf-8"
    )
    return root


def test_selection_is_read_only_and_persists_machine_local_path(tmp_path):
    root = make_locale(tmp_path / "外部工具 with spaces")
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    settings = LocaleEmulatorSettings(tmp_path / "private/settings.json")
    assert not settings.public()["valid"]
    public = settings.save(str(root))
    assert public["valid"] and public["version"] == "2.5.0.1"
    assert public["directory"] == str(root.resolve())
    assert "files" not in public
    assert settings.snapshot()["directory"] == str(root.resolve())
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}


def test_bad_selection_does_not_replace_working_settings(tmp_path):
    root = make_locale(tmp_path / "LE")
    settings = LocaleEmulatorSettings(tmp_path / "settings.json")
    settings.save(str(root))
    previous = settings.path.read_bytes()
    for choice in ("", "relative", str(root / "LEProc.exe"), str(tmp_path / "missing")):
        with pytest.raises(ValueError):
            settings.save(choice)
        assert settings.path.read_bytes() == previous
    (root / "LoaderDll.dll").unlink()
    with pytest.raises(ValueError, match="LoaderDll"):
        settings.save(str(root))
    assert settings.path.read_bytes() == previous


def test_changed_components_require_explicit_reselection(tmp_path):
    root = make_locale(tmp_path / "LE")
    settings = LocaleEmulatorSettings(tmp_path / "settings.json")
    settings.save(str(root))
    with (root / "LEProc.exe").open("ab") as stream:
        stream.write(b"changed")
    assert not settings.public()["valid"]
    with pytest.raises(ValueError, match="发生变化"):
        settings.snapshot()
    assert settings.save(str(root))["valid"]


def test_non_pe_components_and_network_paths_rejected(tmp_path):
    root = make_locale(tmp_path / "LE")
    (root / "LEProc.exe").write_bytes(b"x" * 128)
    with pytest.raises(ValueError, match="Windows"):
        locale_support.inspect_directory(str(root))
    with pytest.raises(ValueError, match="网络"):
        locale_support.inspect_directory(r"\\server\tools")


def test_launcher_uses_independent_profile_and_external_tool(tmp_path):
    root = make_locale(tmp_path / "local LE")
    settings = LocaleEmulatorSettings(tmp_path / "settings.json")
    settings.save(str(root))
    game = tmp_path / "playable"
    exe = game / "game/test.exe"
    profile = ET.fromstring(locale_support.japanese_profile()).find("Profiles/Profile")
    assert profile.findtext("Location") == "ja-JP"
    assert profile.findtext("RunAsAdmin") == "false"
    assert profile.findtext("RunWithSuspend") == "false"
    manifest = {
        "locale_settings_path": str(settings.path),
        "executable": "game/test.exe",
        "files": {"game/test.exe.le.config": "verified"},
    }
    assert locale_command(game, manifest, exe) == [
        str(root / "LEProc.exe"),
        "-run",
        str(exe),
    ]
    manifest["files"] = {}
    with pytest.raises(ValueError, match="profile"):
        locale_command(game, manifest, exe)
    assert (
        locale_command(game, {}, exe)[1] == "-runas"
    )  # Existing packages remain runnable.


def test_legacy_verified_bundle_ignores_new_machine_settings(tmp_path, monkeypatch):
    external = make_locale(tmp_path / "new-machine-LE")
    settings = LocaleEmulatorSettings(tmp_path / "settings.json")
    settings.save(str(external))
    monkeypatch.setenv("NAGI_LOCALE_SETTINGS", str(settings.path))
    game = tmp_path / "legacy-playable"
    executable = game / "game/legacy.exe"
    manifest = {
        "executable": "game/legacy.exe",
        "files": {name: "verified" for name in LEGACY_LOCALE_FILES},
    }

    command = locale_command(game, manifest, executable)

    assert command[0] == str(game / "game/locale-fix/LE/LEProc.exe")
    assert command[1] == "-runas"


def test_locale_http_settings_require_csrf_and_validate_directory(
    tmp_path, monkeypatch
):
    from nagi import webapp

    root = make_locale(tmp_path / "LE")
    monkeypatch.setattr(webapp, "_project_root", lambda: tmp_path)
    server = webapp.QlieWebServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(body):
        return request.urlopen(
            request.Request(
                base + "/api/settings/locale-emulator",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Origin": base},
            )
        )

    try:
        with request.urlopen(base + "/api/settings/locale-emulator") as response:
            assert not json.load(response)["configured"]
        with pytest.raises(error.HTTPError) as failed:
            post({"directory": str(root)})
        assert failed.value.code == 403
        with post(
            {"directory": str(root), "csrf_token": server.csrf_token}
        ) as response:
            assert json.load(response)["valid"]
        with pytest.raises(error.HTTPError) as failed:
            post({"directory": "missing", "csrf_token": server.csrf_token})
        assert failed.value.code == 400
        assert webapp._locale_settings().public()["valid"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
