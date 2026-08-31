"""Locations shared by source, wheel and portable installations.

The application directory contains code only. User state is never a release input.
Legacy state remains in place and is reused without rewriting task receipts.
"""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
from pathlib import Path

APP_ID = "nagi-workbench"
DATA_VERSION = 1


def application_root():
    override = os.environ.get("NAGI_APP_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    package = Path(__file__).resolve().parent
    if (package.parent / "pyproject.toml").is_file():
        return package.parent
    return Path.cwd().resolve()


def resource_root():
    return Path(__file__).resolve().parent


def data_root():
    override = os.environ.get("NAGI_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    root = application_root()
    preference = root / "nagi-data-dir.txt"
    if not preference.is_file() and os.environ.get("LOCALAPPDATA"):
        key = hashlib.sha256(str(root).lower().encode("utf-8")).hexdigest()
        preference = Path(os.environ["LOCALAPPDATA"]) / "Nagi/locations" / (key + ".txt")
    if preference.is_file():
        path = Path(preference.read_text(encoding="utf-8-sig").strip()).expanduser()
        return (path if path.is_absolute() else root / path).resolve()
    if (root / "portable.json").is_file():
        return root / "data"
    if (root / "pyproject.toml").is_file():
        return root
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "Nagi"


def workspace_state(workspace):
    root = Path(workspace)
    modern, legacy = root / ".nagi", root / ".pico"
    # Reusing old state preserves session identities, approvals and encrypted keys.
    # Never copy a private research/model/game tree during automatic migration.
    return legacy if legacy.is_dir() and not modern.exists() else modern


def state_root():
    return workspace_state(data_root())


def translation_root():
    override = os.environ.get("NAGI_TRANSLATION_OUTPUT_ROOT") or os.environ.get("NAGI_QLIE_WEB_OUTPUT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    root = data_root()
    # Preserve the existing source checkout's adjacent translation/playable trees.
    if (root / "pyproject.toml").is_file():
        return root.parent / "translations"
    return root / "translations"


def ensure_data_directory():
    root = data_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=root):
            pass
        state_root().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"数据目录不可写：{root}。请在启动器选择可写目录，或设置 NAGI_DATA_DIR 后重试。"
        ) from exc
    marker = state_root() / "data-version.json"
    if marker.exists():
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        if metadata.get("schema_version") != DATA_VERSION:
            raise ValueError("数据版本比此程序更新或无效；请使用新版 Nagi，不会覆盖数据。")
    else:
        metadata = {"schema_version": DATA_VERSION, "app_id": APP_ID}
        marker.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return root
