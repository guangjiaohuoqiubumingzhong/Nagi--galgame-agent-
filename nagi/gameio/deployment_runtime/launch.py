"""Local-only launcher; keep Unicode drawing attached to the exact game copy."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

if __package__:
    from .locale_support import load_settings
else:
    # The Windows embeddable runtime uses python313._pth and intentionally does
    # not add an executed script's directory to sys.path.  Load the deployed
    # sibling by its exact path instead of relying on normal script semantics.
    from importlib.util import module_from_spec, spec_from_file_location

    _locale_path = Path(__file__).resolve().with_name("locale_support.py")
    _locale_spec = spec_from_file_location("_nagi_deployed_locale_support", _locale_path)
    if _locale_spec is None or _locale_spec.loader is None:
        raise ImportError(f"无法加载部署运行组件：{_locale_path}")
    _locale_module = module_from_spec(_locale_spec)
    _locale_spec.loader.exec_module(_locale_module)
    load_settings = _locale_module.load_settings


LEGACY_LOCALE_FILES = {
    "game/locale-fix/LE/LECommonLibrary.dll",
    "game/locale-fix/LE/LEConfig.xml",
    "game/locale-fix/LE/LEProc.exe",
    "game/locale-fix/LE/LEVersion.xml",
    "game/locale-fix/LE/LoaderDll.dll",
    "game/locale-fix/LE/LocaleEmulator.dll",
}


def process_path(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        length = ctypes.c_uint(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return Path(buffer.value).resolve()
    finally:
        kernel.CloseHandle(handle)


def has_visible_window(pid):
    """Avoid attaching while QLIE is still under Locale Emulator's loader lock."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    visitor_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    user.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user.EnumWindows.argtypes = [visitor_type, ctypes.c_void_p]
    found = []

    @visitor_type
    def visit(window, _):
        owner = ctypes.c_uint()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(window):
            found.append(window)
        return True

    user.EnumWindows(visit, None)
    return bool(found)
    return None


def verified_manifest(root):
    manifest = json.loads((root / "deployment.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported deployment manifest")
    for relative, expected in manifest["files"].items():
        path = root / relative
        if path.is_symlink() or root not in path.resolve().parents:
            raise ValueError("Unsafe deployment file path")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError(f"Deployment changed: {relative}")
    executable = (root / manifest["executable"]).resolve()
    if (
        root not in executable.parents
        or manifest["executable"] not in manifest["files"]
    ):
        raise ValueError("Executable is not part of the verified game copy")
    return manifest, executable


def locale_command(root, manifest, executable):
    settings_path = os.environ.get("NAGI_LOCALE_SETTINGS") or manifest.get("locale_settings_path")
    profile = manifest.get("executable", "") + ".le.config"
    files = set(manifest.get("files", {}))
    if settings_path and profile in files:
        settings = load_settings(settings_path)
        return [
            str(Path(settings["directory"]) / "LEProc.exe"),
            "-run",
            str(executable),
        ]
    if settings_path and not LEGACY_LOCALE_FILES <= files:
        raise ValueError("The independent Locale Emulator profile is not verified")
    # Already published game copies retain their original, verified components.
    return [
        str(root / "game/locale-fix/LE/LEProc.exe"),
        "-runas",
        "ce45c71e-d5c6-4cea-be1a-82f0b7d55cf1",
        str(executable),
    ]


def run(root, check_only=False):
    import frida

    manifest, expected = verified_manifest(root)
    command = locale_command(root, manifest, expected)
    if check_only:
        print(json.dumps({"valid": True, "translated": manifest["translated_count"]}))
        return
    # A named mutex prevents duplicate helpers for this particular copy, without
    # terminating unrelated games or acting on another path's PID.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex_name = (
        "Local\\NagiTranslation-" + hashlib.sha256(str(root).encode()).hexdigest()[:24]
    )
    mutex = kernel.CreateMutexW(None, False, mutex_name)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        print("This localized game is already running.")
        return
    session = None
    try:
        device = frida.get_local_device()

        def find():
            return [
                p.pid
                for p in device.enumerate_processes()
                if p.name == expected.name and process_path(p.pid) == expected
            ]

        pids = find()
        if pids:
            raise RuntimeError(
                "Close the manually started game copy, then use this launcher."
            )
        subprocess.Popen(
            command,
            cwd=str(expected.parent),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            pids = find()
            if pids:
                break
            time.sleep(0.2)
        if len(pids) != 1:
            raise RuntimeError(
                "Game copy did not start uniquely; no other process was touched."
            )
        pid = pids[0]
        if process_path(pid) != expected:
            raise RuntimeError("Game process changed before attachment")
        if manifest.get("engine") == "qlie":
            print("Waiting for the QLIE game window before font attachment.", flush=True)
            deadline = time.monotonic() + 45
            while not has_visible_window(pid):
                if time.monotonic() >= deadline or process_path(pid) != expected:
                    raise RuntimeError("QLIE did not open a game window; close the test copy and check the local runtime.")
                time.sleep(0.2)
        done, ready = threading.Event(), threading.Event()
        errors = []
        session = device.attach(pid)
        session.on("detached", lambda *_: done.set())
        glyphs = json.loads((root / "display-map.json").read_text(encoding="utf-8"))[
            "glyphs"
        ]
        bridge = manifest.get("display_bridge", "display_bridge.js")
        if bridge not in {"display_bridge.js", "qlie_font_bridge.js"} or "runtime/" + bridge not in manifest["files"]:
            raise ValueError("Unverified display bridge")
        source = (
            (root / "runtime" / bridge)
            .read_text(encoding="utf-8")
            .replace("PILOT_GLYPH_MAP", json.dumps(glyphs, ensure_ascii=True), 1)
        )
        script = session.create_script(source)

        def message(value, _data):
            if value.get("type") == "error":
                errors.append(value.get("description", "Font bridge failed"))
                ready.set()
            if value.get("payload", {}).get("type") == "ready":
                ready.set()

        script.on("message", message)
        script.load()
        if not ready.wait(10) or errors:
            raise RuntimeError("Font bridge was not ready: " + "; ".join(errors))
        print(
            "Chinese font bridge ready. Close the game normally when finished.",
            flush=True,
        )
        while not done.is_set():
            if errors:
                raise RuntimeError("; ".join(errors))
            status = dict(
                script.exports_sync.status(),
                pid=pid,
                executable=str(expected),
                ready=True,
            )
            (root / "runtime-status.json").write_text(
                json.dumps(status), encoding="utf-8"
            )
            done.wait(2)
    finally:
        if session is not None:
            try:
                session.detach()
            except frida.InvalidOperationError:
                pass
        kernel.CloseHandle(mutex)


def start_background(root):
    """Detach only this helper; leave the invoking terminal and game untouched."""
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(root), "--background"],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )


def report_failure(root, exc, *, show_dialog):
    log_path = root / "launch-error.log"
    try:
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        detail = f"错误日志：{log_path}"
    except OSError:
        detail = "无法写入错误日志，请检查目录权限。"
    message = f"汉化启动器运行失败：{exc}\n\n{detail}"
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if show_dialog:
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.MessageBoxW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        ]
        user.MessageBoxW.restype = ctypes.c_int
        user.MessageBoxW(None, message, "Nagi 汉化启动器", 0x10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--check", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument(
        "--console", action="store_true", help="Keep console output for debugging"
    )
    args = parser.parse_args()
    root = args.directory.resolve()
    try:
        # The .cmd wrapper can exit immediately. The child keeps Frida attached
        # for the game's lifetime, using the same interpreter and dependencies.
        if sys.platform == "win32" and not (
            args.check or args.background or args.console
        ):
            start_background(root)
            return 0
        run(root, args.check)
    except Exception as exc:  # noqa: BLE001 - show actionable startup failures
        report_failure(
            root,
            exc,
            show_dialog=sys.platform == "win32" and not (args.check or args.console),
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
