"""Credential-free installation status, onboarding and recovery information."""
import importlib.util
import json
import sys

from . import __version__
from .paths import APP_ID, application_root, data_root, state_root, translation_root


def capabilities():
    present = lambda name: importlib.util.find_spec(name) is not None
    path = state_root() / "web" / "onboarding.json"
    try:
        accepted = json.loads(path.read_text(encoding="utf-8")).get("acknowledged") is True
    except (OSError, ValueError):
        accepted = False
    return {
        "app_id": APP_ID, "version": __version__, "first_run": not accepted,
        "paths": {"application": str(application_root()), "data": str(data_root()),
                  "configuration": str(state_root()), "translations": str(translation_root()),
                  "playable": str(translation_root().parent / "playable"),
                  "log": str(state_root() / "web" / "server.log")},
        "features": {"agent": True, "translation": True, "bm25": True,
                     "mcp": present("mcp") and present("jsonschema"),
                     "deployment": sys.platform == "win32" and present("frida"),
                     "semantic_runtime": all(present(name) for name in ("onnxruntime", "tokenizers", "numpy"))},
        "locale_emulator_url": "https://github.com/xupefei/Locale-Emulator/releases",
    }


def acknowledge(payload):
    if payload.get("acknowledged") is not True:
        raise ValueError("请确认使用自己的 API 账户、密钥及调用费用。")
    path = state_root() / "web" / "onboarding.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "acknowledged": True}), encoding="utf-8")
    return {"ok": True}
