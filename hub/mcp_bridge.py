"""Expose the official Hub's tools as a server-side Streamable HTTP endpoint."""

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

    @server.list_tools()
    async def list_tools():
        response = await client.get("/tools")
        response.raise_for_status()
        return [types.Tool.model_validate(tool) for tool in response.json()]

    @server.call_tool()
    async def call_tool(name, arguments):
        response = await client.post(
            "/tools/call_tool", json={**arguments, "name": name}
        )
        response.raise_for_status()
        # NOTE: The official Hub returns only content, dropping downstream isError.
        # The MCP SDK marks this call successful unless the bridge raises an error.
        result = types.CallToolResult.model_validate({"content": response.json()})
        return result.content

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with client:
            yield

    return Starlette(
        routes=[Route("/mcp", MCPTransport(server), methods=["POST", "GET", "DELETE"])],
        lifespan=lifespan,
    )
