import base64
import json
import subprocess
import sys

import pytest

from nagi import directory_picker as picker


@pytest.mark.parametrize("purpose", ["game", "storage", "locale", "workspace"])
def test_windows_dialog_uses_sta_without_console_or_tk(monkeypatch, purpose):
    selected = "D:\\游戏目录\\中文 & 空格 '测试'"

    def run(command, windows):
        assert windows is True
        assert command[0].replace("\\", "/").lower().endswith("windowspowershell/v1.0/powershell.exe")
        assert "-STA" in command
        assert "-NoProfile" in command
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        title = base64.b64encode(picker._TITLES[purpose].encode()).decode()
        assert title in script
        assert "System.Windows.Forms.FolderBrowserDialog" in script
        assert "ShowNewFolderButton = $false" in script
        assert "ShowDialog($owner)" in script
        return subprocess.CompletedProcess(command, 0, json.dumps({"path": selected}))

    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(picker, "_run_dialog", run)
    assert picker.choose_directory(purpose) == selected


@pytest.mark.parametrize("path", [None, ""])
def test_cancel_returns_none(monkeypatch, path):
    monkeypatch.setattr(picker, "_run_dialog", lambda command, windows:
                        subprocess.CompletedProcess(command, 0, json.dumps({"path": path})))
    assert picker.choose_directory() is None


@pytest.mark.parametrize("failure", [
    OSError("missing executable"),
    subprocess.CalledProcessError(1, ["picker"], stderr="dialog initialization failed"),
    subprocess.TimeoutExpired(["picker"], picker._DIALOG_TIMEOUT),
    RuntimeError("目录选择窗口未能显示，已自动取消。"),
])
def test_process_failures_are_readable_and_release_lock(monkeypatch, failure):
    def run(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(picker, "_run_dialog", run)
    with pytest.raises(RuntimeError, match="目录"):
        picker.choose_directory()
    assert not picker._PICKER_LOCK.locked()


@pytest.mark.parametrize("output", ["not JSON", "[]", "{}", '{"path":123}'])
def test_bad_output_is_readable_and_releases_lock(monkeypatch, output):
    monkeypatch.setattr(picker, "_run_dialog", lambda command, windows:
                        subprocess.CompletedProcess(command, 0, output))
    with pytest.raises(RuntimeError, match="手动填写"):
        picker.choose_directory()
    assert not picker._PICKER_LOCK.locked()


def test_only_one_native_dialog_opens_at_a_time():
    picker._PICKER_LOCK.acquire()
    try:
        with pytest.raises(RuntimeError, match="已有目录选择窗口"):
            picker.choose_directory()
    finally:
        picker._PICKER_LOCK.release()


@pytest.mark.parametrize("purpose", ["invalid", None, []])
def test_invalid_purpose_does_not_launch_dialog(purpose):
    with pytest.raises(ValueError, match="用途"):
        picker.choose_directory(purpose)


def test_non_windows_tk_runs_outside_http_worker(monkeypatch):
    def run(command, windows):
        assert windows is False
        assert command[:2] == [sys.executable, str(picker.Path(picker.__file__).resolve())]
        assert command[2] == picker._TITLES["game"]
        return subprocess.CompletedProcess(command, 0, '{"path":"/tmp/game"}')

    monkeypatch.setattr(picker.sys, "platform", "linux")
    monkeypatch.setattr(picker, "_run_dialog", run)
    assert picker.choose_directory() == "/tmp/game"


def test_invisible_windows_picker_is_terminated_quickly(monkeypatch):
    class Process:
        pid = 1234
        returncode = None
        terminated = False
        killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def communicate(self, timeout=None):
            self.returncode = 1
            return "", ""

    process = Process()
    monkeypatch.setattr(picker.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(picker, "_visible_picker_window", lambda _pid: False)
    monkeypatch.setattr(picker, "_WINDOW_VISIBILITY_TIMEOUT", 0)
    with pytest.raises(RuntimeError, match="未能显示"):
        picker._run_windows_dialog(["picker"])
    assert process.terminated and not process.killed
