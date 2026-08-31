"""Local provider settings. Public projections never contain credential values."""

import base64
import copy
import ctypes
import ipaddress
import json
import os
import re
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

from .providers import AnthropicCompatibleModelClient, OpenAIChatCompatibleModelClient

PRESETS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash-vision-exp",
        ],
        "docs_url": "https://api-docs.deepseek.com/",
    },
    "kimi": {
        "name": "Kimi · 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k2.6", "kimi-k2.5"],
        "docs_url": "https://platform.kimi.com/docs/overview",
    },
    "glm": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-5.2", "glm-5.3", "glm-4.7"],
        "docs_url": "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
    },
    "qwen": {
        "name": "通义千问 · 阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen3.8-max"],
        "docs_url": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
    },
}
_SETTINGS_LOCK = threading.RLock()
_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def validate_base_url(value):
    value = str(value or "").strip().rstrip("/")
    if len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("API 地址格式不正确")
    parsed = urllib.parse.urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "\\" in value
    ):
        raise ValueError("API 地址不能包含用户名、密码、查询参数或片段")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("远程 API 必须使用 HTTPS；仅本机地址允许 HTTP")
    if parsed.path.endswith(("/chat/completions", "/messages", "/responses")):
        raise ValueError(
            "请填写 API 基础地址，不要包含 chat/completions、messages 或 responses"
        )
    # Force validation of malformed or out-of-range ports.
    _ = parsed.port
    return value


def _origin(value):
    parsed = urllib.parse.urlsplit(value)
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def _protect_secret(value, decrypt=False):
    """Use Windows user-bound DPAPI; other OSes rely on owner-only file permissions."""
    if not value:
        return ""
    if os.name != "nt":
        if decrypt:
            if not value.startswith("local:"):
                raise ValueError("此密钥来自其他系统，请重新输入")
            return value[6:]
        return "local:" + value
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    if decrypt and not value.startswith("dpapi:"):
        raise ValueError("密钥存储格式不支持，请重新输入")
    raw = (
        base64.b64decode(value[6:], validate=True) if decrypt else value.encode("utf-8")
    )
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
    ):
        raise ValueError("本机密钥加解密失败，请使用当前 Windows 用户重新输入密钥")
    try:
        data = ctypes.string_at(target.data, target.size)
        return (
            data.decode("utf-8")
            if decrypt
            else "dpapi:" + base64.b64encode(data).decode("ascii")
        )
    finally:
        kernel32.LocalFree(target.data)


class ModelSettings:
    def __init__(self, path, legacy_loader):
        self.path = Path(path)
        self.legacy_loader = legacy_loader

    def _load(self):
        if not self.path.exists():
            legacy = self.legacy_loader()
            return {
                "revision": "initial",
                "active_provider": "deepseek",
                "providers": [
                    {
                        "id": "deepseek",
                        "kind": "deepseek",
                        "name": "DeepSeek",
                        "model": legacy["model"],
                        "base_url": validate_base_url(legacy["base_url"]),
                        "protocol": "anthropic"
                        if "/anthropic" in legacy["base_url"]
                        else "openai-chat",
                        "json_output": True,
                        "key_source": "environment",
                    }
                ],
            }
        if self.path.is_symlink() or self.path.stat().st_size > 1024 * 1024:
            raise ValueError("模型配置文件不安全或过大")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or document.get("schema_version", 1) != 1
                or not isinstance(document["providers"], list)
                or not isinstance(document["revision"], str)
            ):
                raise TypeError
            identities = set()
            for entry in document["providers"]:
                if not isinstance(entry, dict) or any(
                    not isinstance(entry.get(key), str)
                    for key in ("id", "kind", "name", "model", "base_url", "protocol")
                ):
                    raise ValueError
                if (
                    not _ID.fullmatch(entry["id"])
                    or entry["id"] in identities
                    or entry["kind"] not in {*PRESETS, "custom"}
                ):
                    raise ValueError
                if entry["protocol"] not in {
                    "anthropic",
                    "openai-chat",
                } or not isinstance(entry.get("json_output"), bool):
                    raise ValueError
                if not isinstance(entry.get("credential", ""), str) or entry.get(
                    "key_source", "local"
                ) not in {"local", "environment"}:
                    raise ValueError
                validate_base_url(entry["base_url"])
                identities.add(entry["id"])
            if (
                document.get("active_provider") is not None
                and document["active_provider"] not in identities
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("模型配置文件无效；未覆盖原文件") from exc
        return document

    def _write(self, document):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document["schema_version"] = 1
        document["revision"] = uuid4().hex
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False
            ) as handle:
                temporary = handle.name
                os.chmod(temporary, 0o600)
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def _key(self, entry):
        if entry.get("key_source") == "environment":
            return self.legacy_loader().get("api_key", "")
        return _protect_secret(entry.get("credential", ""), decrypt=True)

    def _public(self, document):
        entries = []
        for entry in document["providers"]:
            value = {
                key: entry[key]
                for key in (
                    "id",
                    "kind",
                    "name",
                    "model",
                    "base_url",
                    "protocol",
                    "json_output",
                )
            }
            try:
                value["api_key_configured"] = bool(self._key(entry))
                value["credential_error"] = None
            except ValueError:
                value["api_key_configured"] = False
                value["credential_error"] = "密钥属于其他 Windows 用户或电脑，或已损坏；请重新输入密钥"
            value["models"] = list(
                dict.fromkeys(
                    [entry["model"], *PRESETS.get(entry["kind"], {}).get("models", [])]
                )
            )
            value["key_source"] = entry.get("key_source", "local")
            entries.append(value)
        return {
            "revision": document["revision"],
            "active_provider": document.get("active_provider"),
            "providers": entries,
            "presets": [
                {"kind": kind, **copy.deepcopy(spec)} for kind, spec in PRESETS.items()
            ],
            "credential_storage": "Windows 用户加密"
            if os.name == "nt"
            else "仅当前用户可读的本机文件",
        }

    def public(self):
        with _SETTINGS_LOCK:
            return self._public(self._load())

    def _entry_config(self, entry):
        config = {
            key: entry[key]
            for key in (
                "id",
                "kind",
                "name",
                "model",
                "base_url",
                "protocol",
                "json_output",
            )
        }
        try:
            config["api_key"] = self._key(entry)
        except ValueError:
            config["api_key"] = ""
        config.update(
            provider=entry["kind"], api_key_configured=bool(config["api_key"])
        )
        return config

    def active_config(self):
        with _SETTINGS_LOCK:
            document = self._load()
            entry = next(
                (
                    item
                    for item in document["providers"]
                    if item["id"] == document.get("active_provider")
                ),
                None,
            )
            if entry is None:
                return {
                    "provider": "",
                    "model": "未选择模型",
                    "base_url": "",
                    "api_key": "",
                    "api_key_configured": False,
                }
            return self._entry_config(entry)

    def _check_revision(self, document, expected):
        if expected != document["revision"]:
            raise ValueError("模型设置已在其他页面修改，请重新打开设置后再试")

    def _candidate(self, document, values):
        if not isinstance(values, dict):
            raise TypeError("提供方配置必须为对象")
        identity = values.get("id")
        if identity is not None and (
            not isinstance(identity, str) or not _ID.fullmatch(identity)
        ):
            raise ValueError("提供方标识无效")
        existing = next(
            (entry for entry in document["providers"] if entry["id"] == identity), None
        )
        if identity and not existing:
            raise ValueError("提供方已不存在，请重新打开设置")
        entry = copy.deepcopy(existing or {})
        kind = values.get("kind", entry.get("kind", "custom"))
        if kind not in {*PRESETS, "custom"} or (existing and kind != existing["kind"]):
            raise ValueError("提供方类型无效")
        defaults = PRESETS.get(kind, {})
        for key, default, maximum in [
            ("name", defaults.get("name", "自定义提供方"), 80),
            ("model", "", 200),
        ]:
            value = values.get(key, entry.get(key, default))
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > maximum
                or any(ord(char) < 32 for char in value)
            ):
                raise ValueError("提供方名称或模型 ID 无效")
            entry[key] = value.strip()
        base = validate_base_url(
            values.get("base_url", entry.get("base_url", defaults.get("base_url")))
        )
        protocol = values.get("protocol", entry.get("protocol", "openai-chat"))
        if protocol not in {"openai-chat", "anthropic"}:
            raise ValueError("不支持的接口协议")
        key = values.get("api_key", "")
        if (
            not isinstance(key, str)
            or len(key) > 8192
            or any(char.isspace() or ord(char) < 32 for char in key)
        ):
            raise ValueError("API 密钥格式无效，请检查空格或换行")
        if key and len(key) < 8:
            raise ValueError("API 密钥过短")
        if existing and _origin(base) != _origin(existing["base_url"]) and not key:
            raise ValueError(
                "更换 API 域名时必须重新输入密钥，避免把旧密钥发送到其他服务"
            )
        if values.get("clear_key") is True:
            entry.pop("credential", None)
            entry["key_source"] = "local"
        if key:
            entry["credential"] = _protect_secret(key)
            entry["key_source"] = "local"
        json_output = values.get(
            "json_output", entry.get("json_output", kind != "custom")
        )
        if not isinstance(json_output, bool):
            raise TypeError("JSON 输出选项必须为布尔值")
        entry.update(
            id=identity or uuid4().hex[:12],
            kind=kind,
            base_url=base,
            protocol=protocol,
            json_output=json_output,
        )
        return entry

    def save(self, values, expected_revision, activate=False):
        if not isinstance(activate, bool):
            raise TypeError("启用选项必须为布尔值")
        with _SETTINGS_LOCK:
            document = self._load()
            self._check_revision(document, expected_revision)
            entry = self._candidate(document, values)
            if activate and not self._key(entry):
                raise ValueError("请先填写 API 密钥再启用")
            document["providers"] = [
                item for item in document["providers"] if item["id"] != entry["id"]
            ] + [entry]
            if activate:
                document["active_provider"] = entry["id"]
            self._write(document)
            return self._public(document)

    def activate(self, identity, model, expected_revision):
        with _SETTINGS_LOCK:
            document = self._load()
            self._check_revision(document, expected_revision)
            entry = next(
                (item for item in document["providers"] if item["id"] == identity), None
            )
            if entry is None:
                raise ValueError("提供方不存在")
            if not self._key(entry):
                raise ValueError("请先配置 API 密钥")
            candidate = self._candidate(document, {"id": identity, "model": model})
            entry.update(candidate)
            document["active_provider"] = identity
            self._write(document)
            return self._public(document)

    def remove(self, identity, expected_revision):
        with _SETTINGS_LOCK:
            document = self._load()
            self._check_revision(document, expected_revision)
            if not any(entry["id"] == identity for entry in document["providers"]):
                raise ValueError("提供方不存在")
            document["providers"] = [
                entry for entry in document["providers"] if entry["id"] != identity
            ]
            if document.get("active_provider") == identity:
                document["active_provider"] = None
            self._write(document)
            return self._public(document)

    def test_config(self, values, expected_revision):
        with _SETTINGS_LOCK:
            document = self._load()
            self._check_revision(document, expected_revision)
            return self._entry_config(self._candidate(document, values))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("API 返回重定向，已停止请求以保护密钥。请检查 API 基础地址")


def _safe_urlopen(request, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


class _RedactedClient:
    def complete(self, *args, **kwargs):
        try:
            return super().complete(*args, **kwargs).replace(self.api_key, "<redacted>")
        except Exception as exc:  # noqa: BLE001 - credential boundary
            raise RuntimeError(str(exc).replace(self.api_key, "<redacted>")) from None


class ManagedChatClient(_RedactedClient, OpenAIChatCompatibleModelClient):
    pass


class ManagedAnthropicClient(_RedactedClient, AnthropicCompatibleModelClient):
    pass


def client_from_config(config, *, translation=False, timeout=300):
    if not config.get("api_key"):
        raise ValueError("请在设置 → 模型中选择提供方并配置 API 密钥")
    kind = config.get("kind", config.get("provider", "custom"))
    base = validate_base_url(config["base_url"])
    protocol = config.get(
        "protocol", "anthropic" if base.endswith("/anthropic") else "openai-chat"
    )
    common = {
        "model": config["model"],
        "base_url": base,
        "api_key": config["api_key"],
        "temperature": 0.1 if translation else 0.2,
        "timeout": timeout,
        "request_opener": _safe_urlopen,
    }
    thinking = {"type": "disabled"} if kind in {"deepseek", "glm"} else None
    extra = {}
    if kind == "kimi":
        common["temperature"] = None
        if config["model"].startswith(("kimi-k2.5", "kimi-k2.6")):
            thinking = {"type": "disabled"}
        elif config["model"].startswith("kimi-k3"):
            extra["reasoning_effort"] = "low"
    if kind == "qwen":
        extra["enable_thinking"] = False
    if kind == "custom":
        common["temperature"] = None
    if protocol == "anthropic" and not (translation and kind == "deepseek"):
        return ManagedAnthropicClient(**common, thinking=thinking)
    if kind == "deepseek":
        common["base_url"] = base.removesuffix("/anthropic")
    return ManagedChatClient(
        **common,
        thinking=thinking,
        extra_body=extra,
        response_format={"type": "json_object"}
        if translation and config.get("json_output", True)
        else None,
    )
