"""Expose the official Hub's tools as a server-side Streamable HTTP endpoint."""

import json
from contextlib import asynccontextmanager

import anyio
import httpx
from scp import types
from scp.server.lowlevel import Server
from scp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Receive, Scope, Send


class MCPTransport:
    def __init__(self, server: Server):
        self.server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        transport = StreamableHTTPServerTransport(
            mcp_session_id=None, is_json_response_enabled=True
        )
        async with (
            transport.connect() as (read, write),
            anyio.create_task_group() as group,
        ):
            group.start_soon(
                self.server.run,
                read,
                write,
                self.server.create_initialization_options(),
                False,
                True,
            )
            await transport.handle_request(scope, receive, send)
            group.cancel_scope.cancel()


def create_app(hub: str) -> Starlette:
    server = Server("SCP Hub")
    client = httpx.AsyncClient(base_url=hub, timeout=40, trust_env=False)

    def tool_error(code: str, message: str, tool: str, **details: object) -> ValueError:
        return ValueError(
            json.dumps(
                {"ok": False, "code": code, "error": message, "tool": tool, **details}
            )
        )

    @server.list_tools()
    async def list_tools():
        response = await client.get("/tools")
        response.raise_for_status()
        return [types.Tool.model_validate(tool) for tool in response.json()]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            response = await client.post(
                "/tools/call_tool", json={**arguments, "name": name}
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise tool_error(
                "hub_timeout",
                "Timed out waiting for the Hub response",
                name,
                error_type=type(error).__name__,
                next_action=(
                    "Check task state before retrying a state-changing call; "
                    "the downstream operation may still have completed."
                ),
            ) from error
        except httpx.HTTPStatusError as error:
            raise tool_error(
                "hub_http_error",
                f"Hub returned HTTP {error.response.status_code}",
                name,
                status_code=error.response.status_code,
                next_action=(
                    "Inspect Hub and downstream logs for the cause. "
                    "Check task state before retrying a state-changing call."
                ),
            ) from error
        except httpx.RequestError as error:
            raise tool_error(
                "hub_transport_error",
                str(error) or type(error).__name__,
                name,
                error_type=type(error).__name__,
                next_action="Check the Hub connection and task state.",
            ) from error
        try:
            result = types.CallToolResult.model_validate({"content": response.json()})
        except ValueError as error:
            raise tool_error(
                "hub_invalid_response",
                "Hub returned an invalid tool response",
                name,
                next_action="Inspect Hub and downstream logs.",
            ) from error
        # The official Hub drops isError. HEPAnalysis preserves errors in its
        # explicit JSON envelope; raising lets the MCP SDK restore isError.
        if name.startswith("HEPAnalysis."):
            for block in result.content:
                if not isinstance(block, types.TextContent):
                    continue
                try:
                    value = json.loads(block.text)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("ok") is False:
                    raise ValueError(block.text)
        return result.content

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with client:
            yield

    return Starlette(
        routes=[Route("/mcp", MCPTransport(server), methods=["POST", "GET", "DELETE"])],
        lifespan=lifespan,
    )
