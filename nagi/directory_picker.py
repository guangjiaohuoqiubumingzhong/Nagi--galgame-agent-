"""Isolated desktop folder dialogs; never load Tcl/Tk in an HTTP worker."""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_PICKER_LOCK = threading.Lock()
_DIALOG_TIMEOUT = 600
_WINDOW_VISIBILITY_TIMEOUT = 8
_TITLES = {
    "game": "选择游戏目录",
    "storage": "选择文本保存目录",
    "locale": "选择 Locale Emulator 目录（包含 LEProc.exe）",
    "workspace": "选择工作区目录",
}
_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$title = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__TITLE__'))
$owner = New-Object System.Windows.Forms.Form
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
try {
    $owner.Text = $title
    $owner.ShowInTaskbar = $false
    $owner.TopMost = $true
    $owner.Opacity = 0
    $owner.Width = 1
    $owner.Height = 1
    $owner.StartPosition = 'CenterScreen'
    $dialog.Description = $title
    $dialog.ShowNewFolderButton = $false
    $owner.Show()
    $owner.Activate()
    $selected = $null
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        $selected = $dialog.SelectedPath
    }
    @{ path = $selected } | ConvertTo-Json -Compress
} finally {
    $dialog.Dispose()
    $owner.Dispose()
}
"""


def _windows_picker(title):
    # No shell interpolation of user paths, no console window, and an STA main
    # thread for the native Windows dialog. Windows PowerShell ships with Windows.
    title_data = base64.b64encode(title.encode("utf-8")).decode("ascii")
    script = _WINDOWS_SCRIPT.replace("__TITLE__", title_data)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    executable = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
                  / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    return [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
            "-EncodedCommand", encoded]


def _visible_picker_window(process_id):
    """Return True only after the native folder dialog has a visible HWND."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    found = False
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int

    @callback_type
    def inspect(handle, _state):
        nonlocal found
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value != process_id or not user32.IsWindowVisible(handle):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, class_name, len(class_name))
        # WinForms FolderBrowserDialog is a native dialog. Ignore the invisible
        # WindowsForms owner used only to place it above the browser.
        if class_name.value == "#32770":
            found = True
            return False
        return True

    user32.EnumWindows(inspect, 0)
    return found


def _stop_process(process):
    process.terminate()
    try:
        return process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()


def _run_windows_dialog(command):
    """Run a native picker, but never let an invisible dialog retain the lock."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    visibility_deadline = time.monotonic() + _WINDOW_VISIBILITY_TIMEOUT
    while process.poll() is None:
        if _visible_picker_window(process.pid):
            break
        if time.monotonic() >= visibility_deadline:
            _stop_process(process)
            raise RuntimeError(
                "目录选择窗口未能显示，已自动取消。请从桌面快捷方式启动 Nagi 后重试，"
                "或手动填写完整目录路径。"
            )
        time.sleep(0.05)
    try:
        stdout, stderr = process.communicate(timeout=_DIALOG_TIMEOUT)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode, command, output=stdout, stderr=stderr
        )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _run_dialog(command, windows):
    if windows:
        return _run_windows_dialog(command)
    return subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=_DIALOG_TIMEOUT,
        creationflags=0,
    )


def choose_directory(purpose="game"):
    """Return a folder or None on cancellation; surface failures as API errors."""
    if not isinstance(purpose, str) or purpose not in _TITLES:
        raise ValueError("无效的目录选择用途。")
    if not _PICKER_LOCK.acquire(blocking=False):
        raise RuntimeError("已有目录选择窗口打开，请先完成或取消该窗口。")
    try:
        title = _TITLES[purpose]
        windows = sys.platform == "win32"
        command = (_windows_picker(title) if windows else
                   [sys.executable, str(Path(__file__).resolve()), title])
        try:
            result = _run_dialog(command, windows)
            payload = json.loads(result.stdout.lstrip("\ufeff"))
            if not isinstance(payload, dict):
                raise TypeError("invalid directory dialog response")
            if "path" not in payload:
                raise ValueError("missing directory dialog path")
            selected = payload["path"]
            if selected is not None and not isinstance(selected, str):
                raise TypeError("invalid directory dialog path")
            return selected or None
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("目录选择已超时，请重新点击“选择”，或手动填写完整目录路径。") from exc
        except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            logging.getLogger(__name__).warning(
                "Directory dialog failed (%s, exit=%s): %s", type(exc).__name__,
                getattr(exc, "returncode", None), getattr(exc, "stderr", None) or str(exc),
            )
            raise RuntimeError("无法打开目录选择窗口，请手动填写完整目录路径。") from exc
    finally:
        _PICKER_LOCK.release()


def _tk_picker(title):
    # Non-Windows fallback runs on a separate process's main thread. Missing Tk
    # or display support cannot abort a web request or the Nagi server.
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        return filedialog.askdirectory(parent=root, title=title, mustexist=True) or None
    finally:
        root.destroy()


if __name__ == "__main__":
    print(json.dumps({"path": _tk_picker(sys.argv[1])}, ensure_ascii=True))
