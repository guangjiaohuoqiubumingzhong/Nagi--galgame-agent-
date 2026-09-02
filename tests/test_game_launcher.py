import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from nagi.game_launcher import repair_runtime
from nagi.gameio.deployment_runtime import launch


def test_standalone_launcher_loads_sibling_under_isolated_python(tmp_path):
    runtime = tmp_path / "已部署 runtime"
    runtime.mkdir()
    source = Path(launch.__file__).resolve().parent
    for name in ("launch.py", "locale_support.py"):
        (runtime / name).write_bytes((source / name).read_bytes())

    result = subprocess.run(
        [sys.executable, "-I", str(runtime / "launch.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_default_launch_detaches_before_starting_game(tmp_path, monkeypatch):
    background, run = Mock(), Mock()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["launch.py", str(tmp_path)])
    monkeypatch.setattr(launch, "start_background", background)
    monkeypatch.setattr(launch, "run", run)
    assert launch.main() == 0
    background.assert_called_once_with(tmp_path.resolve())
    run.assert_not_called()


@pytest.mark.parametrize("option", ["--background", "--console", "--check"])
def test_worker_debug_and_check_do_not_spawn_again(tmp_path, monkeypatch, option):
    background, run = Mock(), Mock()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["launch.py", str(tmp_path), option])
    monkeypatch.setattr(launch, "start_background", background)
    monkeypatch.setattr(launch, "run", run)
    assert launch.main() == 0
    background.assert_not_called()
    run.assert_called_once_with(tmp_path.resolve(), option == "--check")


@pytest.mark.parametrize(
    "option,dialog",
    [(None, True), ("--background", True), ("--check", False), ("--console", False)],
)
def test_failure_keeps_log_and_reports_without_console(
    tmp_path, monkeypatch, option, dialog
):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        sys, "argv", ["launch.py", str(tmp_path)] + ([option] if option else [])
    )
    monkeypatch.setattr(sys, "stderr", None)
    failure = Mock(side_effect=RuntimeError("test startup failure"))
    monkeypatch.setattr(launch, "start_background", failure)
    monkeypatch.setattr(launch, "run", failure)
    user = Mock()
    monkeypatch.setattr(launch.ctypes, "WinDLL", Mock(return_value=user), raising=False)
    assert launch.main() == 1
    assert "test startup failure" in (tmp_path / "launch-error.log").read_text(
        encoding="utf-8"
    )
    assert user.MessageBoxW.call_count == int(dialog)
    if dialog:
        assert "test startup failure" in user.MessageBoxW.call_args.args[1]


def test_failure_dialog_survives_unwritable_log(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Path, "write_text", Mock(side_effect=PermissionError("read only"))
    )
    user = Mock()
    monkeypatch.setattr(launch.ctypes, "WinDLL", Mock(return_value=user), raising=False)
    launch.report_failure(tmp_path, RuntimeError("bridge failed"), show_dialog=True)
    assert "bridge failed" in user.MessageBoxW.call_args.args[1]
    assert "无法写入错误日志" in user.MessageBoxW.call_args.args[1]


def test_background_uses_same_python_and_no_console(tmp_path, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    monkeypatch.setattr(
        launch.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False
    )
    launch.start_background(tmp_path)
    args, kwargs = popen.call_args
    assert args[0] == [
        sys.executable,
        str(Path(launch.__file__).resolve()),
        str(tmp_path),
        "--background",
    ]
    assert kwargs == {
        "cwd": str(tmp_path),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "creationflags": 0x08000000,
        "close_fds": True,
    }
    popen.return_value.wait.assert_not_called()


def test_repair_runtime_replaces_legacy_absolute_python_launcher(tmp_path):
    root = tmp_path / "playable/YU-RIS/Game/translation-formal"
    (root / "game").mkdir(parents=True)
    (root / "runtime").mkdir()
    executable = root / "game/game.exe"
    executable.write_bytes(b"MZ synthetic")
    legacy = root / "runtime/launch.py"
    legacy.write_text("# old runtime", encoding="utf-8")
    internal = root / "启动汉化版.cmd"
    internal.write_text(
        '@echo off\n"D:\\removed\\pico\\.venv\\Scripts\\python.exe" '
        '"%~dp0runtime\\launch.py" "%~dp0."\n',
        encoding="utf-8",
    )
    digest = lambda path: __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "profile": "legacy-test",
        "translated_count": 1,
        "executable": "game/game.exe",
        "files": {
            "game/game.exe": digest(executable),
            "runtime/launch.py": digest(legacy),
        },
    }
    (root / "deployment.json").write_text(json.dumps(manifest), encoding="utf-8")

    backup = repair_runtime(root)

    assert (backup / "启动汉化版.cmd").is_file()
    repaired = internal.read_text(encoding="utf-8")
    assert "locate_runtime.ps1" in repaired
    assert "removed" not in repaired and "python.exe" not in repaired
    updated = json.loads((root / "deployment.json").read_text(encoding="utf-8"))
    assert "runtime/locale_support.py" in updated["files"]
    assert "runtime/locate_runtime.ps1" in updated["files"]
    outer = root.parent / "启动正式版.cmd"
    assert outer.is_file() and "translation-formal" in outer.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console creation")
def test_actual_background_process_has_no_console(tmp_path, monkeypatch):
    # Exercise the actual Windows process flags without starting a game or Frida.
    root = tmp_path / "路径 with spaces"
    root.mkdir()
    script = root / "probe.py"
    script.write_text(
        "import ctypes, json, sys\n"
        "from pathlib import Path\n"
        "status = dict(console=ctypes.windll.kernel32.GetConsoleWindow(), args=sys.argv[1:])\n"
        "Path('probe.json').write_text(json.dumps(status), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(launch, "__file__", str(script))
    original_popen = subprocess.Popen
    children = []

    def capture(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(launch.subprocess, "Popen", capture)
    launch.start_background(root)
    child = children[0]
    try:
        assert child.wait(timeout=10) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    status = json.loads((root / "probe.json").read_text(encoding="utf-8"))
    assert status == {"console": 0, "args": [str(root), "--background"]}
