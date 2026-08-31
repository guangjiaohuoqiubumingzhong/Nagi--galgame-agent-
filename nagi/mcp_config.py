"""Local, opt-in MCP configuration. Reading configuration never starts a server."""

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from .paths import workspace_state
from urllib.parse import urlsplit


class MCPError(ValueError):
    """An error with a public, credential-free message."""


@dataclass(repr=False)
class ServerConfig:
    name: str
    transport: str
    command: str = ""
    args: list = field(default_factory=list)
    cwd: str = ""
    env: dict = field(default_factory=dict)
    url: str = ""
    headers: dict = field(default_factory=dict)
    timeout: float = 30
    tools: list | None = None


class MCPConfig:
    def __init__(self, root, path=None):
        self.servers = {}
        self.errors = []
        self.secrets = set()
        if path is False:
            return
        explicit = path or os.environ.get("NAGI_MCP_CONFIG") or os.environ.get("PICO_MCP_CONFIG")
        target = Path(explicit) if explicit else workspace_state(root) / "mcp.json"
        if not target.is_absolute():
            target = Path(root) / target
        try:
            if not target.exists() and not explicit:
                return
            if target.stat().st_size > 256_000:
                raise MCPError("MCP configuration exceeds 256 KB")
            payload = json.loads(target.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or set(payload) != {"mcpServers"}:
                raise MCPError("MCP configuration must contain only an mcpServers object")
            entries = payload["mcpServers"]
            if not isinstance(entries, dict) or len(entries) > 32:
                raise MCPError("mcpServers must be an object with at most 32 servers")
            for name, value in entries.items():
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                    self.errors.append("Invalid MCP server id (use letters, digits, _ or -)")
                    continue
                try:
                    config = self._server(name, value, Path(root))
                    if config:
                        self.servers[name] = config
                except MCPError as exc:
                    self.errors.append(f"{name}: {exc}")
        except MCPError as exc:
            self.errors.append(str(exc))
        except (OSError, ValueError, TypeError):
            self.errors.append("Cannot read MCP configuration; check the path and JSON syntax")

    def _values(self, values):
        if not isinstance(values, dict):
            raise MCPError("env and headers must be string maps")
        result = {}
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise MCPError("env and headers must be string maps")

            def expand(match):
                resolved = os.environ.get(match[1])
                if resolved is None:
                    raise MCPError("A referenced environment variable is missing")
                if resolved:
                    self.secrets.add(resolved)
                return resolved

            result[key] = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", expand, value)
            if result[key]:
                self.secrets.add(result[key])
        return result

    def _server(self, name, value, root):
        if not isinstance(value, dict):
            raise MCPError("Server configuration must be an object")
        if not isinstance(value.get("disabled", False), bool):
            raise MCPError("disabled must be a boolean")
        if value.get("disabled"):
            return None
        transport = value.get("transport", "http" if "url" in value else "stdio")
        if transport == "streamable-http":
            transport = "http"
        if not isinstance(transport, str) or transport not in {"stdio", "http"}:
            raise MCPError("Supported transports are stdio and http (Streamable HTTP)")
        common = {"transport", "disabled", "timeout", "tools"}
        fields = {"command", "args", "cwd", "env"} if transport == "stdio" else {"url", "headers"}
        if set(value) - common - fields:
            raise MCPError("Unknown or incompatible server configuration fields")
        timeout = value.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 300:
            raise MCPError("timeout must be between 0.1 and 300 seconds")
        allowed = value.get("tools")
        if allowed is not None and (not isinstance(allowed, list) or any(not isinstance(t, str) or not t for t in allowed)):
            raise MCPError("tools must be a list of allowed tool names")
        config = ServerConfig(name, transport, timeout=timeout, tools=allowed)
        if transport == "stdio":
            command = value.get("command")
            args = value.get("args", [])
            cwd = value.get("cwd", ".")
            if not isinstance(command, str) or not command.strip():
                raise MCPError("stdio requires an executable command")
            if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                raise MCPError("args must be a string array")
            if not isinstance(cwd, str):
                raise MCPError("cwd must be a path string")
            config.command, config.args = command, args
            config.cwd = str((root / cwd).resolve())
            config.env = self._values(value.get("env", {}))
        else:
            url = value.get("url")
            if not isinstance(url, str):
                raise MCPError("http requires a URL")
            try:
                parsed = urlsplit(url)
                host = parsed.hostname
                _ = parsed.port
                loopback = host == "localhost"
                if host and not loopback:
                    try:
                        loopback = ipaddress.ip_address(host).is_loopback
                    except ValueError:
                        pass
                if (not host or parsed.username or parsed.password or parsed.query or parsed.fragment
                        or parsed.scheme not in {"http", "https"}
                        or (parsed.scheme == "http" and not loopback)):
                    raise ValueError()
            except ValueError:
                raise MCPError("Use HTTPS (or loopback HTTP), without URL credentials, query or fragment") from None
            config.url = url
            config.headers = self._values(value.get("headers", {}))
            if any(k.lower() in {"host", "content-type", "accept", "content-length"}
                   or k.lower().startswith("mcp-") for k in config.headers):
                raise MCPError("Do not override MCP transport headers")
        return config

    def redact(self, text):
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
            text = text.replace(json.dumps(secret, ensure_ascii=True)[1:-1], "[REDACTED]")
            text = text.replace(secret, "[REDACTED]")
        return text

    def redact_artifact(self, value):
        if isinstance(value, dict):
            return {self.redact(str(key)): self.redact_artifact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_artifact(item) for item in value]
        return self.redact(value) if isinstance(value, str) else value
