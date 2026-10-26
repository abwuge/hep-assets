"""Expose an SCP Hub's discovered tools over standard MCP stdio."""

import argparse
import asyncio
import json

import httpx
from scp import types
from scp.server.lowlevel import Server
from scp.server.stdio import stdio_server


async def serve(url, server_name):
    server = Server("SCPHub")
    async with httpx.AsyncClient(base_url=url, timeout=40, trust_env=False) as client:
        response = await client.get("/tools", params={"server_name": server_name})
        response.raise_for_status()
        tools = response.json()
        # This connection selects one server; its namespace belongs on the Hub wire.
        names = {tool["name"].removeprefix(server_name + "."): tool["name"] for tool in tools}

        @server.list_tools()
        async def list_tools():
            return [types.Tool.model_validate({**tool, "name": name})
                    for name, tool in zip(names, tools)]

        @server.call_tool()
        async def call_tool(name, arguments):
            response = await client.post("/tools/call_tool", json={
                **arguments, "name": names[name],
            })
            response.raise_for_status()
            blocks = response.json()
            if not isinstance(blocks, list):
                raise ValueError(json.dumps(blocks))
            result = []
            for block in blocks:
                if block["type"] == "text":
                    result.append(types.TextContent.model_validate(block))
                elif block["type"] == "image":
                    result.append(types.ImageContent.model_validate(block))
                else:
                    raise ValueError(f"Unsupported Hub content: {block['type']}")
            return result

        async with stdio_server() as streams:
            await server.run(*streams, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True)
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.hub, args.server))


if __name__ == "__main__":
    main()
