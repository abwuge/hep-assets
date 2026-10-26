"""Test Hub startup configuration and MCP forwarding through the official Hub."""

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import httpx
from scp import ClientSession, StdioServerParameters, types
from scp.client.sse import sse_client
from scp.client.stdio import stdio_client
from scp.server.fastmcp import FastMCP

HUB_DIR = Path(__file__).resolve().parents[1]
IMAGE = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
    "AwMCAO+aD1sAAAAASUVORK5CYII="
)


def serve_downstream(port: int):
    server = FastMCP("Test", host="127.0.0.1", port=port)

    @server.tool()
    def echo(message: str) -> list[types.TextContent | types.ImageContent]:
        """Return text and an image unchanged."""
        return [
            types.TextContent(type="text", text=message),
            types.ImageContent(type="image", data=IMAGE, mimeType="image/png"),
        ]

    @server.tool()
    def fail() -> str:
        """Return a tool execution error."""
        raise ValueError("deliberate tool failure")

    server.run(transport="sse")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class StartupTests(unittest.TestCase):
    def test_project_local_state_and_bind_address(self):
        spec = importlib.util.spec_from_file_location("hub_run", HUB_DIR / "run.py")
        assert spec is not None and spec.loader is not None
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        upstream = ModuleType("scp.hub.hub_server")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(launcher, "__file__", str(Path(directory) / "run.py")),
            patch.dict(sys.modules, {"scp.hub.hub_server": upstream}),
            patch.dict(os.environ),
            patch.object(upstream, "run", create=True) as run,
        ):
            os.environ.pop("REDIS_HOST", None)
            launcher.main()
            state = Path(directory).resolve() / ".local"
            self.assertTrue(state.is_dir())
            self.assertEqual(
                os.environ["STORAGE_FILE_PATH"], str(state / "servers.json")
            )
            self.assertEqual(os.environ["REDIS_HOST"], "127.0.0.1")
            run.assert_called_once_with(host="127.0.0.1", port=46380)
            os.environ["REDIS_HOST"] = "configured-redis"
            launcher.main()
            self.assertEqual(os.environ["REDIS_HOST"], "configured-redis")


class ForwardingTests(unittest.IsolatedAsyncioTestCase):
    def start_service(self, args: list[str], port: int, env: dict[str, str]):
        log = tempfile.TemporaryFile(mode="w+t")
        self.addCleanup(log.close)
        process = subprocess.Popen(
            [sys.executable, *args], env=env, stdout=log, stderr=log
        )

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        self.addCleanup(stop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        log.seek(0)
        self.fail(f"Service failed to start:\n{log.read()}")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "servers.json"
        env = {**os.environ, "STORAGE_FILE_PATH": str(self.state)}
        self.downstream_port = free_port()
        self.start_service(
            [str(Path(__file__).resolve()), "--serve", str(self.downstream_port)],
            self.downstream_port,
            env,
        )
        port = free_port()
        self.start_service(
            [
                "-c",
                "from scp.hub.hub_server import run; "
                f"run(host='127.0.0.1', port={port})",
            ],
            port,
            env,
        )
        self.hub = f"http://127.0.0.1:{port}"

    async def asyncSetUp(self):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                f"{self.hub}/register_server",
                json={
                    "server_name": "Test",
                    "server_description": "Test tools",
                    "server_url": "http://127.0.0.1",
                    "server_port": self.downstream_port,
                    "type": "sse",
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(self.state.is_file())

    @asynccontextmanager
    async def session(self):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(HUB_DIR / "mcp_bridge.py"), "--hub", self.hub],
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=15)
            ) as client,
        ):
            await client.initialize()
            yield client

    async def test_discovery_and_content(self):
        async with self.session() as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertIn("Test.echo", tools)
            self.assertIn("Test.fail", tools)
            schema = tools["Test.echo"].inputSchema
            self.assertEqual(schema["properties"]["message"]["type"], "string")
            self.assertEqual(schema["required"], ["message"])
            result = await client.call_tool("Test.echo", {"message": "你好，Hub"})
            self.assertFalse(result.isError)
            self.assertEqual(
                result.content,
                [
                    types.TextContent(type="text", text="你好，Hub"),
                    types.ImageContent(type="image", data=IMAGE, mimeType="image/png"),
                ],
            )

    async def test_http_error_becomes_mcp_error(self):
        async with self.session() as client:
            result = await client.call_tool("Missing.echo", {})
            self.assertTrue(result.isError)
            self.assertIn("404", str(result.content))

    async def test_upstream_drops_tool_error_flag(self):
        async with (
            sse_client(f"http://127.0.0.1:{self.downstream_port}/sse") as (read, write),
            ClientSession(read, write) as downstream,
        ):
            await downstream.initialize()
            original = await downstream.call_tool("fail", {})
            self.assertTrue(original.isError)
        async with self.session() as client:
            result = await client.call_tool("Test.fail", {})
            # Document the official Hub's current content-only response contract.
            self.assertFalse(result.isError)
            self.assertEqual(result.content, original.content)
            self.assertIn("deliberate tool failure", str(result.content))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve_downstream(int(sys.argv[2]))
    else:
        unittest.main()
