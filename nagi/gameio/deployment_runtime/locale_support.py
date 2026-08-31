"""Local Locale Emulator selection, shared by Nagi and standalone launchers.

Selection only reads files. Never downloads, installs, or runs the selected tool.
"""

from __future__ import annotations

import hashlib
import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

BINARIES = ("LEProc.exe", "LECommonLibrary.dll", "LoaderDll.dll", "LocaleEmulator.dll")
DOWNLOAD_URL = "https://github.com/xupefei/Locale-Emulator/releases"


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_directory(directory):
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError("请在 Nagi 翻译页选择本地 Locale Emulator 目录。")
    selected = Path(directory.strip()).expanduser()
    if not selected.is_absolute() or str(selected).startswith("\\\\"):
        raise ValueError("请选择本机的完整目录路径，不支持网络共享目录。")
    if selected.is_symlink() or (
        hasattr(selected, "is_junction") and selected.is_junction()
    ):
        raise ValueError("Locale Emulator 目录不能是符号链接或目录联接。")
    root = selected.resolve()
    if not root.is_dir():
        raise ValueError("Locale Emulator 目录不存在，请重新选择。")
    checksums = {}
    for name in BINARIES:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise ValueError(
                f"Locale Emulator 缺少必需组件：{name}；请选择完整解压后的目录。"
            )
        if not 64 <= path.stat().st_size <= 64 * 1024 * 1024:
            raise ValueError(f"Locale Emulator 组件大小异常：{name}")
        with path.open("rb") as stream:
            header = stream.read(64)
            offset = struct.unpack_from("<I", header, 60)[0]
            if header[:2] != b"MZ" or offset > path.stat().st_size - 6:
                raise ValueError(f"不是有效的 Windows 组件：{name}")
            stream.seek(offset)
            if stream.read(4) != b"PE\0\0":
                raise ValueError(f"不是有效的 Windows 组件：{name}")
        checksums[name] = file_hash(path)
    version = "未知版本"
    version_file = root / "LEVersion.xml"
    if (
        version_file.is_file()
        and not version_file.is_symlink()
        and version_file.stat().st_size < 65536
    ):
        try:
            version = ET.fromstring(version_file.read_bytes()).get("Version", version)
        except ET.ParseError:
            pass
    return {
        "schema_version": 1,
        "directory": str(root),
        "files": checksums,
        "version": version,
    }


def load_settings(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("尚未配置 Locale Emulator，请在 Nagi 翻译页选择目录并保存。")
    saved = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(saved, dict) or saved.get("schema_version") != 1:
        raise ValueError("Locale Emulator 配置无效，请重新选择目录并保存。")
    current = inspect_directory(saved.get("directory"))
    if current["files"] != saved.get("files"):
        raise ValueError(
            "Locale Emulator 组件已发生变化，请在 Nagi 中重新选择并保存后再启动。"
        )
    return current


def save_settings(path, directory):
    settings = inspect_directory(directory)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return settings


def public_settings(path):
    try:
        settings = load_settings(path)
        return {
            "configured": True,
            "valid": True,
            "directory": settings["directory"],
            "version": settings["version"],
            "message": "组件检查通过；日区配置由 Nagi 单独生成。",
            "download_url": DOWNLOAD_URL,
        }
    except (OSError, ValueError, TypeError) as exc:
        return {
            "configured": Path(path).is_file(),
            "valid": False,
            "directory": "",
            "version": None,
            "message": str(exc),
            "download_url": DOWNLOAD_URL,
        }


def japanese_profile():
    root = ET.Element("LEConfig")
    profiles = ET.SubElement(root, "Profiles")
    profile = ET.SubElement(
        profiles, "Profile", Name="Nagi Japanese", Guid=str(uuid4()), MainMenu="false"
    )
    for name, value in {
        "Parameter": "",
        "Location": "ja-JP",
        "Timezone": "Tokyo Standard Time",
        "RunAsAdmin": "false",
        "RedirectRegistry": "true",
        "IsAdvancedRedirection": "false",
        "RunWithSuspend": "false",
    }.items():
        ET.SubElement(profile, name).text = value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
