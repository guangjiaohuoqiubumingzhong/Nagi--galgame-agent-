"""Engine-independent, relative launch entries in each playable game's folder."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .paths import application_root, data_root, resource_root


def write_runtime_launcher(directory):
    """Bind a playable copy to a relocatable Nagi installation, never to Python."""
    directory = Path(directory).resolve()
    app = application_root()
    def relative(target, start):
        try:
            return os.path.relpath(target, start)
        except ValueError:
            return None
    locator = {"schema_version": 1, "app_root": str(app),
               "relative_app": relative(app, directory),
               "relative_data": relative(data_root(), app)}
    (directory / "nagi-runtime.json").write_text(json.dumps(locator, indent=2), encoding="utf-8")
    runtime = directory / "runtime"
    runtime.mkdir(exist_ok=True)
    template = resource_root() / "gameio/deployment_runtime/locate_runtime.ps1"
    (runtime / template.name).write_bytes(template.read_bytes())
    prefix = '@rem Nagi runtime launcher v1\r\n@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
    command = ('"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
               '-NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0runtime\\locate_runtime.ps1"')
    (directory / "启动汉化版.cmd").write_bytes((prefix + command + '\r\n').encode("utf-8"))
    (directory / "重新定位Nagi.cmd").write_bytes((prefix + command + ' -Relocate\r\n').encode("utf-8"))

_MARKER = b"@rem Nagi managed game launcher v1\r\n"
_LABELS = {"full": "正式版", "partial": "测试版", "pilot": "测试版"}


def launcher_path(game_root, target, mode):
    root, target = Path(game_root).resolve(), Path(target).resolve()
    if root not in target.parents or target.parent == root or not target.is_file():
        raise ValueError("启动目标必须是游戏根目录下已生成的版本内文件")
    if target.suffix.lower() not in {".cmd", ".bat", ".exe", ".ps1"}:
        raise ValueError("不支持的 Windows 游戏启动文件类型")
    if mode not in _LABELS:
        raise ValueError("Invalid launcher translation mode")
    return root / f"启动{_LABELS[mode]}.cmd"


def deployment_fingerprint(target):
    target = Path(target)
    manifest = target.parent / "deployment.json"
    return hashlib.sha256((manifest if manifest.is_file() else target).read_bytes()).hexdigest()


def launcher_content(game_root, target):
    root, target = Path(game_root).resolve(), Path(target).resolve()
    relative = target.relative_to(root)
    # Percent signs in literal batch source need escaping. Quoting plus disabled
    # delayed expansion protects spaces, &, parentheses, and !. Do not use CALL:
    # its second expansion would reinterpret percent signs in directory names.
    def batch_path(path):
        value = str(path).replace("/", "\\")
        if any(char in value for char in ('"', "\r", "\n", "\x00")):
            raise ValueError("启动路径包含不能安全写入批处理的字符")
        return value.replace("%", "%%")

    path = "%~dp0" + batch_path(relative)
    directory = "%~dp0" + batch_path(relative.parent)
    lines = [
        "@echo off", "setlocal DisableDelayedExpansion", "chcp 65001 >nul",
        f'if not exist "{path}" (',
        "  echo 汉化版本的启动文件已移动或删除，请从 Nagi 重新生成入口。",
        "  pause", "  exit /b 1", ")",
        f'pushd "{directory}"', "if errorlevel 1 exit /b 1",
    ]
    if target.suffix.lower() == ".ps1":
        lines.append('"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
                     f'-NoLogo -NoProfile -File "{path}"')
    else:
        lines.append(f'"{path}"')
    lines.append("exit /b %errorlevel%")
    return _MARKER + ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _check_managed(path):
    if path.is_symlink() or (path.exists() and (not path.is_file() or not path.read_bytes().startswith((_MARKER, b"@rem Pico managed game launcher v1\r\n")))):
        raise ValueError(f"不会覆盖已有的非 Nagi 启动文件：{path.name}")


def _write_entry(path, content):
    _check_managed(path)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_launcher(game_root, target, mode):
    """Publish only the test or formal entry for this deployment.

    No engine-specific paths or runtime dependencies are copied. Each wrapper
    delegates to the unchanged launcher shipped with that deployment.
    """
    version = launcher_path(game_root, target, mode)
    content = launcher_content(game_root, target)
    _write_entry(version, content)
    return version


def validate_version_launcher(game_root, target, mode, entry):
    """Restore a workflow's exact test/formal entry."""
    expected = launcher_path(game_root, target, mode)
    entry = Path(entry)
    if entry.is_symlink() or entry.resolve() != expected or not entry.is_file():
        raise ValueError("Launcher escapes its version-specific game entry")
    if entry.read_bytes() != launcher_content(game_root, target):
        raise ValueError("游戏根目录启动入口已改变，请重新生成入口")
    return expected


def repair_runtime(directory):
    """Explicit repair command; verify first, back up launch metadata, keep saves."""
    import shutil

    from .gameio.deployment_runtime.launch import verified_manifest
    from .paths import state_root

    if Path(directory).is_symlink():
        raise ValueError("拒绝修改符号链接游戏目录")
    root = Path(directory).resolve()
    manifest, _ = verified_manifest(root)
    backup = root / ("nagi-launcher-backup-" + uuid4().hex[:8])
    backup.mkdir()
    for name in (
        "deployment.json",
        "启动汉化版.cmd",
        "nagi-runtime.json",
        "runtime/launch.py",
        "runtime/locale_support.py",
        "runtime/locate_runtime.ps1",
    ):
        source = root / name
        if source.is_symlink():
            raise ValueError("拒绝修改符号链接启动文件")
        if source.is_file():
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    for relative in ("launch.py", "locale_support.py"):
        target = root / "runtime" / relative
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(
            (resource_root() / "gameio/deployment_runtime" / relative).read_bytes()
        )
        manifest["files"]["runtime/" + relative] = hashlib.sha256(
            target.read_bytes()
        ).hexdigest()
    if manifest.get("locale_settings_path"):
        manifest["locale_settings_path"] = str(state_root() / "web/locale-emulator.json")
    write_runtime_launcher(root)
    locator = root / "runtime/locate_runtime.ps1"
    manifest["files"]["runtime/locate_runtime.ps1"] = hashlib.sha256(
        locator.read_bytes()
    ).hexdigest()
    (root / "deployment.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # Refresh the game-level test/formal entry as well, including Pico-era
    # managed launchers that still point at this version directory.
    mode = {"translation-test": "partial", "translation-formal": "full"}.get(
        root.name
    )
    if mode:
        publish_launcher(root.parent, root / "启动汉化版.cmd", mode)
    verified_manifest(root)
    return backup


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Repair a verified Nagi playable copy's runtime launcher")
    parser.add_argument("action", choices=["repair"])
    parser.add_argument("directory")
    options = parser.parse_args()
    print(f"启动入口已更新，备份：{repair_runtime(options.directory)}")
