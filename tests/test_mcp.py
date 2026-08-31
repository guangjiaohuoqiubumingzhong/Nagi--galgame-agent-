import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nagi import FakeModelClient, Nagi, SessionStore, WorkspaceContext
from nagi.mcp_config import MCPConfig
from nagi.mcp_tools import MCP_TOOL_SPECS, MCPToolService

HAS_SDK = all(importlib.util.find_spec(name) for name in ("mcp", "jsonschema"))
sdk = pytest.mark.skipif(not HAS_SDK, reason='install ".[mcp]" to run MCP integration tests')
SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"


def config_file(root, entry):
    path = root / ".nagi" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"test": entry}}), encoding="utf-8")
    return path


def stdio_config(**extra):
    return {"command": sys.executable, "args": [str(SERVER)], "timeout": 10, **extra}


def make_agent(root, entry=None, **kwargs):
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("native content", encoding="utf-8")
    if entry is not None:
        config_file(root, entry)
    return Nagi(model_client=kwargs.pop("model_client", FakeModelClient([])),
                workspace=WorkspaceContext.build(root), session_store=SessionStore(root / ".nagi" / "sessions"),
                approval_policy=kwargs.pop("approval_policy", "auto"), **kwargs)


def discover(agent):
    result = agent.execute_tool("mcp_list_tools", {"server": "test"})
    assert result.metadata["tool_status"] == "ok", result.content
    return json.loads(result.content)


def call(agent, tool="read_file", arguments=None):
    return agent.execute_tool("mcp_call_tool", {"server": "test", "tool": tool,
                                              "arguments": arguments if arguments is not None else {"entry": {"count": 7}}})


def test_unconfigured_or_invalid_config_keeps_native_tools(tmp_path, monkeypatch):
    monkeypatch.delenv("NAGI_MCP_CONFIG", raising=False)
    with make_agent(tmp_path) as agent:
        assert json.loads(agent.run_tool("mcp_list_servers", {}))["servers"] == []
        assert "native content" in agent.run_tool("read_file", {"path": "README.md"})
    config_file(tmp_path, {"command": 42})
    with make_agent(tmp_path) as agent:
        assert json.loads(agent.run_tool("mcp_list_servers", {}))["configuration_errors"]
        assert "native content" in agent.run_tool("read_file", {"path": "README.md"})


@pytest.mark.parametrize("extra", [{"url": "http://example.com/mcp"}, {"url": "https://u:p@example.com/mcp"},
                                  {"url": "https://example.com/mcp?token=x"}, {"transport": "sse"},
                                  {"command": "python", "timeout": 0}, {"command": "python", "args": "--help"},
                                  {"command": "python", "env": {"key": "${MISSING_NAGI_MCP_TEST_VAR}"}}])
def test_invalid_server_is_not_enabled(tmp_path, extra):
    path = config_file(tmp_path, extra)
    config = MCPConfig(tmp_path, path)
    assert not config.servers
    assert config.errors


def test_config_no_side_effects_and_approval_before_connection(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("nagi.mcp_client.MCPConnection.connect", lambda self: calls.append(True))
    with make_agent(tmp_path, stdio_config(), approval_policy="never") as agent:
        agent.run_tool("mcp_list_servers", {})
        result = agent.execute_tool("mcp_list_tools", {"server": "test"})
        assert result.metadata["tool_error_code"] == "approval_denied"
        assert not calls


def test_without_optional_dependencies_local_tools_still_work(tmp_path):
    config_file(tmp_path, stdio_config())
    code = """
import sys
from nagi import Nagi, FakeModelClient, WorkspaceContext, SessionStore
root = sys.argv[1]
with Nagi(FakeModelClient([]), WorkspaceContext.build(root), SessionStore(root + '/.nagi/sessions'), approval_policy='auto') as agent:
    assert 'error:' not in agent.run_tool('list_files', {'path': '.'})
    assert 'dependencies missing' in agent.run_tool('mcp_list_tools', {'server': 'test'})
assert 'mcp' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-S", "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr


@sdk
@pytest.mark.parametrize("mode", ["paged", "duplicate", "cycle", "invalid_schema", "external_ref"])
def test_discovery_pagination_and_schema_boundaries(tmp_path, monkeypatch, mode):
    from mcp.types import ListToolsResult, Tool

    operations = []
    closed = []

    class Connection:
        def __init__(self, config, _cancel):
            self.config, self.schemas = config, None
            self.owner = SimpleNamespace(done=lambda: False)

        def connect(self):
            operations.append("connect")

        def request(self, operation, cursor=None):
            operations.append((operation, cursor))
            schema = {"type": "object", "properties": {"count": {"type": "integer"}},
                      "required": ["count"], "additionalProperties": False}
            if mode == "invalid_schema":
                schema["type"] = "invalid-type"
            if mode == "external_ref":
                schema = {"type": "object", "properties": {"count": {"$ref": "https://must-not-fetch.example/schema"}}}
            start = 8 if cursor and mode != "duplicate" else 0
            return ListToolsResult(tools=[Tool(name=f"tool_{i}", inputSchema=schema) for i in range(start, start + 8)],
                                   nextCursor="next" if not cursor or mode == "cycle" else None)

        def close(self):
            self.schemas = None
            closed.append(True)

    monkeypatch.setattr("nagi.mcp_tools.MCPConnection", Connection)
    with make_agent(tmp_path, stdio_config()) as agent:
        result = agent.execute_tool("mcp_list_tools", {"server": "test"})
        if mode in {"duplicate", "cycle"}:
            assert result.metadata["tool_status"] == "error"
            assert closed
            return
        assert result.metadata["tool_status"] == "ok", result.content
        first = json.loads(result.content)
        assert len(first["tools"]) == 8 and first["next_offset"] == 8
        second = json.loads(agent.run_tool("mcp_list_tools", {"server": "test", "offset": 8}))
        assert second["next_offset"] is None
        assert operations == ["connect", ("list", None), ("list", "next")]
        if mode == "invalid_schema":
            assert first["tools"][0]["unsupported"] is True
        else:
            for arguments in ({"count": "secret-invalid-type"}, {"count": 1, "extra": True}, {"count": 1} if mode == "external_ref" else {}):
                rejected = call(agent, "tool_0", arguments)
                assert rejected.metadata["tool_error_code"] == "invalid_arguments"
                assert "secret-invalid-type" not in rejected.content
        assert len(operations) == 3  # Invalid arguments never reach the server.


def test_config_override_and_disabled_servers(tmp_path, monkeypatch):
    default = config_file(tmp_path, stdio_config())
    explicit = tmp_path / "alternate.json"
    explicit.write_text(json.dumps({"mcpServers": {"disabled": {"disabled": True}}}), encoding="utf-8")
    monkeypatch.setenv("NAGI_MCP_CONFIG", str(explicit))
    assert not MCPConfig(tmp_path).servers
    assert "test" in MCPConfig(tmp_path, default).servers
    assert not MCPConfig(tmp_path, False).servers


@sdk
@pytest.mark.parametrize("output_schema", [
    {"type": "object", "properties": {"count": {"type": "integer"}}},
    {"type": "object", "properties": {"count": {"$ref": "https://must-not-fetch.example/schema"}}},
])
def test_output_schema_validation_does_not_fetch_remote_references(tmp_path, monkeypatch, output_schema):
    from mcp.types import CallToolResult, Tool

    network = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: network.append(True))
    with make_agent(tmp_path, stdio_config()) as agent:
        tool = Tool(name="read_file", inputSchema={"type": "object"}, outputSchema=output_schema)
        item = agent.mcp_tool_service._schema(tool)
        requests = []

        def request(operation, arguments):
            requests.append(operation)
            return CallToolResult(content=[], structuredContent={"count": "invalid-result"})

        agent.mcp_tool_service.connections["test"] = SimpleNamespace(
            schemas={"read_file": item}, owner=SimpleNamespace(done=lambda: False),
            request=request, close=lambda: None,
        )
        result = call(agent, arguments={})
        assert result.metadata["tool_status"] == "error"
        assert "outputSchema" in result.content
        assert "invalid-result" not in result.content
        assert requests == ["call"]  # Output validation never re-executes a remote action.
        assert not network


@sdk
def test_stdio_schema_namespace_reuse_approval_and_errors(tmp_path):
    with make_agent(tmp_path, stdio_config()) as agent:
        payload = discover(agent)
        assert "read_file" in [item["name"] for item in payload["tools"]]
        schema = json.loads(agent.run_tool("mcp_describe_tool", {"server": "test", "tool": "read_file"}))
        assert "entry" in schema["inputSchema"]["properties"]
        denied = call(agent, arguments={"entry": {"count": "not an integer"}})
        assert denied.metadata["tool_error_code"] == "invalid_arguments"
        first = json.loads(call(agent).content)["structuredContent"]
        second = json.loads(call(agent).content)["structuredContent"]
        assert first["calls"] == 1 and second["calls"] == 2
        assert first["pid"] == second["pid"]
        typed = call(agent, "typed_output", {})
        assert json.loads(typed.content)["structuredContent"]["count"] == 9
        assert "native content" in agent.run_tool("read_file", {"path": "README.md"})
        # Even readOnlyHint=true cannot bypass host approval.
        agent.approval_policy = "never"
        assert call(agent).metadata["tool_error_code"] == "approval_denied"
        agent.approval_policy = "auto"
        failed = call(agent, "failure", {})
        assert failed.metadata["tool_status"] == "error"
        assert failed.metadata["mcp"]["is_error"] is True
        assert failed.metadata["risk_level"] == "high" and failed.metadata["read_only"] is False
        assert json.loads(failed.content)["isError"] is True
        assert json.loads(call(agent, "big_result", {}).content)["truncated"] is True
    assert not agent.mcp_tool_service.connections


@sdk
def test_config_secrets_redacted_and_env_not_inherited(tmp_path, monkeypatch):
    secret = 'private-value-"\\-abcdef'
    monkeypatch.setenv("FIXTURE_CREDENTIAL", secret)
    monkeypatch.setenv("UNRELATED_API_KEY", "must-not-inherit-this")
    with make_agent(tmp_path, stdio_config(env={"NAGI_MCP_FIXTURE_VALUE": "${FIXTURE_CREDENTIAL}"})) as agent:
        discover(agent)
        result = call(agent, "credential_echo", {})
        data = json.loads(result.content)["structuredContent"]
        assert data["value"] == "[REDACTED]"
        assert json.loads(json.loads(result.content)["content"][0]["text"])["value"] == "[REDACTED]"
        assert data["unrelated_secret"] == "not-inherited"
        assert secret not in agent.prefix
        assert agent.redact_artifact({"raw": secret})["raw"] == "[REDACTED]"


@sdk
def test_missing_server_and_allowlist_do_not_break_local_tools(tmp_path):
    with make_agent(tmp_path, stdio_config(command="nagi-test-nonexistent-executable")) as agent:
        result = agent.execute_tool("mcp_list_tools", {"server": "test"})
        assert result.metadata["tool_status"] == "error"
        assert "native content" in agent.run_tool("read_file", {"path": "README.md"})
    with make_agent(tmp_path, stdio_config(tools=["read_file"])) as agent:
        assert len(discover(agent)["tools"]) == 1
        assert call(agent, "failure", {}).metadata["tool_error_code"] == "invalid_arguments"


@sdk
def test_timeout_cancel_and_turn_cleanup(tmp_path):
    with make_agent(tmp_path, stdio_config(), cancel_event=threading.Event()) as agent:
        discover(agent)
        connection = agent.mcp_tool_service.connections["test"]
        connection.config.timeout = 0.15
        started = time.monotonic()
        assert call(agent, "slow", {}).metadata["tool_status"] == "error"
        assert time.monotonic() - started < 8
        assert connection.portal is None
        connection.config.timeout = 10
        discover(agent)
        connection = agent.mcp_tool_service.connections["test"]
        timer = threading.Timer(0.2, agent.cancel_event.set)
        timer.start()
        try:
            assert "cancelled" in call(agent, "slow", {}).content
        finally:
            timer.join()
        assert connection.schemas is None
        assert connection.portal is None
    assert not any(thread.name == "nagi-mcp" for thread in threading.enumerate())
    model = FakeModelClient(['<tool>{"name":"mcp_list_tools","args":{"server":"test"}}</tool>', '<final>done</final>'])
    agent = make_agent(tmp_path, stdio_config(), model_client=model)
    assert agent.ask("discover tools") == "done"
    assert not agent.mcp_tool_service.connections
    assert call(agent).metadata["tool_error_code"] == "invalid_arguments"


@sdk
def test_http_transport(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(SERVER), str(port)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                assert process.poll() is None, "HTTP fixture exited early"
                time.sleep(0.05)
        with make_agent(tmp_path, {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"}) as agent:
            discover(agent)
            result = call(agent)
            assert result.metadata["tool_status"] == "ok", result.content
            assert json.loads(result.content)["structuredContent"]["count"] == 7
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_plan_mode_blocks_external_actions_and_prompt_includes_gateways(tmp_path):
    with make_agent(tmp_path, stdio_config()) as agent:
        prompt = agent.prompt("What tools are available?")
        for name in MCP_TOOL_SPECS:
            assert f"- {name}(" in prompt
        assert "- read_file(" in prompt
        agent.session["plan_mode"] = True
        result = agent.execute_tool("mcp_list_tools", {"server": "test"})
        assert result.metadata["tool_error_code"] == "invalid_arguments"
        assert not agent.mcp_tool_service.connections


@sdk
def test_native_translation_batch_coexists_with_mcp(tmp_path):
    from test_translation_planner import (
        build_translation_tool_agent,
        valid_response_payload,
    )

    agent, translation_model, requests, spec, _ = build_translation_tool_agent(tmp_path)
    config_file(agent.root, stdio_config())
    agent.mcp_tool_service = MCPToolService(agent.root)
    agent.allowed_tools = None
    agent.tools = agent.build_tools()
    agent.refresh_prefix(force=True)
    translation_model.outputs = [json.dumps(valid_response_payload(request), ensure_ascii=False) for request in requests]
    with agent:
        prompt = agent.prompt("Translate and use MCP")
        for name in ("translation_run_status", "translate_game_batch", *MCP_TOOL_SPECS):
            assert f"- {name}(" in prompt
        discover(agent)
        assert call(agent).metadata["tool_status"] == "ok"
        status = json.loads(agent.run_tool("translation_run_status", {"run_id": spec.run_id}))
        assert status["next"]["request_id"] == requests[0].request_id
        result = agent.execute_tool("translate_game_batch", {"run_id": spec.run_id, "request_id": requests[0].request_id})
        assert result.metadata["tool_status"] == "ok", result.content
        assert json.loads(result.content)["reason"] == "batch_completed"
        assert len(translation_model.prompts) == 1
        assert json.loads(call(agent).content)["structuredContent"]["calls"] == 2


@sdk
def test_web_job_uses_workspace_mcp_config_and_existing_approval_ui(tmp_path):
    from nagi.agent_web import AgentWebService

    root = tmp_path / "workspace"
    root.mkdir()
    config_file(root, stdio_config())
    messages = [
        '<tool>{"name":"mcp_list_tools","args":{"server":"test"}}</tool>',
        '<tool>{"name":"mcp_describe_tool","args":{"server":"test","tool":"read_file"}}</tool>',
        '<tool>{"name":"mcp_call_tool","args":{"server":"test","tool":"read_file","arguments":{"entry":{"count":4}}}}</tool>',
        '<final>MCP complete</final>',
    ]
    service = AgentWebService(tmp_path / "web-state", root, lambda: FakeModelClient(messages))
    job = service.start_job(workspace_path=root, session_id=None, message="Use MCP", approval_policy="ask")
    approved = []
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline and job.status not in {"completed", "failed"}:
            pending = job.public_dict()["pending_approval"]
            if pending and pending["approval_id"] not in approved:
                approved.append(pending["approval_id"])
                job.resolve_approval(pending["approval_id"], True)
            time.sleep(0.02)
        assert job.status == "completed", job.public_dict()
        assert len(approved) == 2
        payload = job.public_dict()
        assert payload["final_answer"] == "MCP complete"
        assert [item["role"] for item in payload["history"]] == ["user", "assistant"]
        assert not job.agent.mcp_tool_service.connections
        assert any(event.get("mcp", {}).get("tool") == "read_file" for event in payload["trace"])
    finally:
        job.cancel()
