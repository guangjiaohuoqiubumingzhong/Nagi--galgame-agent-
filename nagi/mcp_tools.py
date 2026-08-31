"""Namespaced MCP tool gateway; native Nagi/translation tools remain independent."""

import importlib.util
import json
from functools import partial

from .mcp_client import MCPConnection
from .mcp_config import MCPConfig, MCPError
from .tool_executor import ToolExecutionResult

MCP_TOOL_SPECS = {
    "mcp_list_servers": ({}, False, "List configured MCP servers without connecting; local tools remain available."),
    "mcp_list_tools": ({"server": "str", "offset": "int=0"}, True,
                       "Connect with approval and discover external tools (paged, 8 per page)."),
    "mcp_describe_tool": ({"server": "str", "tool": "str"}, False,
                          "Read a discovered tool's inputSchema; no connection. Do this before calling."),
    "mcp_call_tool": ({"server": "str", "tool": "str", "arguments": "dict"}, True,
                      "Call a discovered MCP tool with schema-valid arguments and approval. Never auto-retry failures."),
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _deny_reference(uri):
    raise MCPError("External JSON Schema references are not supported")


class MCPToolService:
    def __init__(self, root, config_path=None, cancel_event=None):
        self.config = MCPConfig(root, config_path)
        self.cancel_event = cancel_event
        self.connections = {}

    def registry(self):
        # A constant namespace avoids collisions, including MCP tools named like local tools.
        return {
            name: {"schema": schema, "risky": risky, "description": description,
                   "run": partial(self.run, name)}
            for name, (schema, risky, description) in MCP_TOOL_SPECS.items()
        }

    def _discovered(self, server, tool):
        connection = self.connections.get(server)
        if not connection or connection.schemas is None or connection.owner.done():
            raise MCPError("Use mcp_list_tools for this server first (discovery expires after each agent turn)")
        if tool not in connection.schemas:
            raise MCPError("Tool is not discovered or is excluded by the configured allowlist")
        item = connection.schemas[tool]
        if item["unsupported"]:
            raise MCPError("This tool has an unsupported or oversized input/output schema")
        return item

    def validate(self, name, args):
        if not isinstance(args, dict):
            raise MCPError("Arguments must be an object")
        required = {
            "mcp_list_servers": set(), "mcp_list_tools": {"server"},
            "mcp_describe_tool": {"server", "tool"}, "mcp_call_tool": {"server", "tool", "arguments"},
        }[name]
        optional = {"offset"} if name == "mcp_list_tools" else set()
        if not required <= set(args) or set(args) - required - optional:
            raise MCPError("Missing or unexpected MCP gateway arguments")
        if name == "mcp_list_servers":
            return
        server = args["server"]
        if not isinstance(server, str) or server not in self.config.servers:
            raise MCPError("Unknown MCP server; use mcp_list_servers to inspect configuration")
        if name == "mcp_list_tools":
            offset = args.get("offset", 0)
            if type(offset) is not int or offset < 0:
                raise MCPError("offset must be a non-negative integer")
            return
        if not isinstance(args["tool"], str):
            raise MCPError("tool must be a string")
        item = self._discovered(server, args["tool"])
        if name == "mcp_call_tool":
            if not isinstance(args["arguments"], dict):
                raise MCPError("arguments must be an object matching inputSchema")
            try:
                item["validator"].validate(args["arguments"])
            except Exception:  # noqa: BLE001 - untrusted schemas can raise many validator errors
                # ValidationError includes instance values: never expose it in history/trace.
                raise MCPError("arguments do not match inputSchema; use mcp_describe_tool") from None

    def _schema(self, tool):
        from jsonschema.validators import validator_for
        from referencing import Registry

        schema = tool.inputSchema
        item = {"name": tool.name, "description": (tool.description or "")[:180],
                "inputSchema": schema, "unsupported": False, "validator": None, "output_validator": None}
        try:
            if len(tool.name) > 128 or len(_json(schema)) > 2800:
                raise ValueError()
            validator = validator_for(schema)
            validator.check_schema(schema)
            item["validator"] = validator(schema, registry=Registry(retrieve=_deny_reference))
            if tool.outputSchema is not None:
                if len(_json(tool.outputSchema)) > 2800:
                    raise ValueError()
                output_validator = validator_for(tool.outputSchema)
                output_validator.check_schema(tool.outputSchema)
                item["output_validator"] = output_validator(tool.outputSchema, registry=Registry(retrieve=_deny_reference))
        except Exception:  # noqa: BLE001 - quarantine invalid external schemas, not native tools
            item["unsupported"] = True
        return item

    def _discover(self, server):
        existing = self.connections.get(server)
        if existing and existing.schemas is not None and not existing.owner.done():
            return existing
        if existing:
            existing.close()
        connection = MCPConnection(self.config.servers[server], self.cancel_event)
        self.connections[server] = connection
        connection.connect()
        found, cursors, cursor = {}, set(), None
        try:
            for _ in range(32):
                page = connection.request("list", cursor)
                for tool in page.tools:
                    if tool.name in found:
                        raise MCPError("MCP server returned duplicate tool names")
                    found[tool.name] = self._schema(tool)
                    if len(found) > 256:
                        raise MCPError("MCP server exceeds the 256-tool discovery limit")
                cursor = page.nextCursor
                if not cursor:
                    break
                if cursor in cursors:
                    raise MCPError("MCP server repeated a pagination cursor")
                cursors.add(cursor)
            else:
                raise MCPError("MCP server exceeds the 32-page discovery limit")
            allowlist = connection.config.tools
            connection.schemas = {key: value for key, value in found.items()
                                  if allowlist is None or key in allowlist}
            return connection
        except BaseException:
            connection.close()
            raise

    def _execute(self, name, args):
        if name == "mcp_list_servers":
            return {"servers": [{"server": config.name, "transport": config.transport}
                                for config in self.config.servers.values()],
                    "configuration_errors": self.config.errors,
                    "dependencies_available": all(importlib.util.find_spec(module) is not None
                                                  for module in ("mcp", "jsonschema"))}, False
        server = args["server"]
        if name == "mcp_list_tools":
            connection = self._discover(server)
            items = list(connection.schemas.values())
            offset = args.get("offset", 0)
            return {"server": server, "tools": [{k: item[k] for k in ("name", "description", "unsupported")}
                                                 for item in items[offset:offset + 8]],
                    "next_offset": offset + 8 if offset + 8 < len(items) else None}, False
        item = self._discovered(server, args["tool"])
        if name == "mcp_describe_tool":
            return {key: item[key] for key in ("name", "description", "inputSchema")}, False
        result = self.connections[server].request("call", (args["tool"], args["arguments"]))
        if not result.isError and item["output_validator"] is not None:
            try:
                if result.structuredContent is None:
                    raise ValueError()
                item["output_validator"].validate(result.structuredContent)
            except Exception:  # noqa: BLE001 - remote schemas/results must not leak validator details
                raise MCPError("MCP result does not match outputSchema. Remote effects may have occurred; do not retry automatically") from None
        # Nagi's model interface is text-only. Never dump base64 or fetch linked resources.
        content = []
        for block in result.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
            elif block.type == "resource" and hasattr(block.resource, "text"):
                content.append({"type": "text", "text": block.resource.text})
            else:
                content.append({"type": block.type, "omitted": "Non-text content; not fetched or rendered"})
        payload = {"server": server, "tool": args["tool"], "isError": result.isError, "content": content}
        if result.structuredContent is not None:
            payload["structuredContent"] = result.structuredContent
        return payload, result.isError

    def run(self, name, args):
        failed = False
        try:
            payload, failed = self._execute(name, args)
        except MCPError as exc:
            payload, failed = {"error": str(exc)}, True
        except Exception:  # noqa: BLE001 - SDK/HTTP exception text can contain credentials
            payload, failed = {"error": "MCP connection or protocol failed; check the server locally. "
                              "Remote effects may already have occurred; do not retry automatically."}, True
        encoded = _json(self.config.redact_artifact(payload))
        # Keep a valid JSON envelope even when the existing tool-output budget is small.
        if len(encoded) > 3900:
            preview = encoded[:3000]
            encoded = _json({"truncated": True, "isError": failed, "preview": preview})
            while len(encoded) > 3900:
                preview = preview[:len(preview) // 2]
                encoded = _json({"truncated": True, "isError": failed, "preview": preview})
        return ToolExecutionResult(encoded, {
            "tool_status": "error" if failed else "ok",
            "tool_error_code": "mcp_failed" if failed else "",
            "mcp": {"server": args.get("server"), "tool": args.get("tool"), "is_error": failed},
        })

    def close(self):
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()
