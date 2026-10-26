"""Expose the official Hub's tools through standard MCP stdio."""

import argparse
import asyncio

import httpx
from scp import types
from scp.server.lowlevel import Server
from scp.server.stdio import stdio_server


async def serve(hub: str):
    server = Server("SCP Hub")
    async with httpx.AsyncClient(base_url=hub, timeout=40, trust_env=False) as client:

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

        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", default="http://127.0.0.1:46380")
    args = parser.parse_args()
    asyncio.run(serve(args.hub))


if __name__ == "__main__":
    main()
