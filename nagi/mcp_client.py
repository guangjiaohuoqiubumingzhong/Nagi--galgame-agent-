"""Synchronous facade over the official async MCP SDK (loaded only on use)."""

import os
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AsyncExitStack, suppress
from datetime import timedelta

from .mcp_config import MCPError


class MCPConnection:
    def __init__(self, config, cancel_event=None):
        self.config = config
        self.cancel_event = cancel_event
        self.portal_context = None
        self.portal = None
        self.owner = None
        self.session = None
        self.schemas = None

    def _wait(self, future):
        deadline = time.monotonic() + self.config.timeout
        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                future.cancel()
                raise MCPError("MCP operation cancelled; remote effects may already have occurred; do not retry automatically")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                raise MCPError("MCP operation timed out; remote effects may already have occurred; do not retry automatically")
            try:
                return future.result(timeout=min(0.05, remaining))
            except FutureTimeout:
                if future.done():
                    raise MCPError("MCP operation timed out; do not retry automatically") from None

    async def _serve(self, ready):
        # Enter and exit SDK contexts in ONE task: anyio cancel scopes are task-local.
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        try:
            async with AsyncExitStack() as stack:
                if self.config.transport == "stdio":
                    # A local null device cannot block on network/disk data; ExitStack owns it.
                    errlog = stack.enter_context(open(os.devnull, "w"))  # noqa: ASYNC230, SIM115
                    streams = await stack.enter_async_context(stdio_client(
                        StdioServerParameters(command=self.config.command, args=self.config.args,
                                              cwd=self.config.cwd, env=self.config.env), errlog=errlog,
                    ))
                else:
                    import httpx
                    client = await stack.enter_async_context(httpx.AsyncClient(
                        headers=self.config.headers, timeout=self.config.timeout,
                        follow_redirects=False, trust_env=False,
                    ))
                    streams = await stack.enter_async_context(streamable_http_client(
                        self.config.url, http_client=client,
                    ))
                self.session = await stack.enter_async_context(ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=self.config.timeout),
                ))
                # No sampling, elicitation or roots callbacks; server instructions are not prompts.
                await self.session.initialize()
                self.stop = anyio.Event()
                if not ready.done():
                    ready.set_result(True)
                await self.stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise

    def connect(self):
        try:
            import jsonschema  # noqa: F401
            import mcp  # noqa: F401
            from anyio.from_thread import start_blocking_portal
        except ImportError:
            raise MCPError('MCP dependencies missing; install with: python -m pip install -e ".[mcp]"') from None
        try:
            self.portal_context = start_blocking_portal(name="nagi-mcp")
            self.portal = self.portal_context.__enter__()
            ready = Future()
            self.owner = self.portal.start_task_soon(self._serve, ready)
            self._wait(ready)
        except BaseException:
            self.close()
            raise

    async def _request(self, operation, arguments):
        if operation == "list":
            return await self.session.list_tools(cursor=arguments)
        from mcp import types

        # Use SDK framing/session handling, but validate output in our restricted registry.
        # ClientSession.call_tool's convenience validation can resolve remote schema refs.
        return await self.session.send_request(
            types.ClientRequest(types.CallToolRequest(params=types.CallToolRequestParams(
                name=arguments[0], arguments=arguments[1],
            ))),
            types.CallToolResult,
        )

    def request(self, operation, arguments=None):
        if not self.portal or self.owner.done():
            raise MCPError("MCP connection closed; discover the server's tools again before calling")
        try:
            return self._wait(self.portal.start_task_soon(self._request, operation, arguments))
        except BaseException:
            self.close()
            raise

    def close(self):
        self.schemas = None
        if self.portal_context is None:
            return
        try:
            if self.owner and not self.owner.done():
                if hasattr(self, "stop"):
                    self.portal.call(self.stop.set)
                else:
                    self.owner.cancel()
                try:
                    self.owner.result(timeout=5)
                except FutureTimeout:
                    self.owner.cancel()
                except Exception:  # noqa: BLE001
                    # The request reports failures; cleanup must not re-expose SDK secrets.
                    self.session = None
        finally:
            with suppress(Exception):
                self.portal_context.__exit__(None, None, None)
            self.portal_context = self.portal = self.session = None
