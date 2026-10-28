"""MCP fallback and machine-only HTTP file transfers."""

import base64
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from scp.types import TextContent

from analysis.execution import read_json, write_json
from analysis.server import create_server
from analysis.tasks import CHUNK_BYTES, TaskManager
from analysis.transfers import http_base_urls


class AddressTests(unittest.TestCase):
    def setUp(self):
        def address(family, value):
            return SimpleNamespace(family=family, address=value)

        self.addresses = {
            "lo": [
                address(socket.AF_INET, "127.0.0.1"),
                address(socket.AF_INET6, "::1"),
            ],
            "nic0": [
                address(socket.AF_INET, "10.0.0.2"),
                address(socket.AF_INET, "198.51.100.9"),
                address(socket.AF_INET6, "2001:db8::2"),
                address(socket.AF_INET6, "fe80::2%nic0"),
                address(socket.AF_INET6, "::"),
                address(socket.AF_INET6, "ff02::1"),
                address(socket.AF_INET, "0.0.0.0"),
                address(socket.AF_INET, "224.0.0.1"),
                address(
                    socket.AF_LINK if hasattr(socket, "AF_LINK") else 17,
                    "00:11:22:33:44:55",
                ),
            ],
            "nic1": [address(socket.AF_INET, "10.0.0.2")],
            "down": [address(socket.AF_INET, "10.0.0.3")],
        }
        self.stats = {
            name: SimpleNamespace(isup=name != "down") for name in self.addresses
        }

    def test_all_active_ipv4_addresses_without_hostname_lookup(self):
        with (
            patch(
                "analysis.transfers.psutil.net_if_addrs", return_value=self.addresses
            ),
            patch("analysis.transfers.psutil.net_if_stats", return_value=self.stats),
            patch("socket.getfqdn", side_effect=AssertionError("No hostname lookup")),
            patch("socket.getaddrinfo", side_effect=AssertionError("No DNS lookup")),
        ):
            self.assertEqual(
                http_base_urls("0.0.0.0", 8001),
                [
                    "http://10.0.0.2:8001",
                    "http://198.51.100.9:8001",
                    "http://127.0.0.1:8001",
                ],
            )

    def test_ipv6_family_scope_and_brackets(self):
        with (
            patch(
                "analysis.transfers.psutil.net_if_addrs", return_value=self.addresses
            ),
            patch("analysis.transfers.psutil.net_if_stats", return_value=self.stats),
        ):
            self.assertEqual(
                http_base_urls("::", 9123),
                [
                    "http://[2001:db8::2]:9123",
                    "http://[fe80::2%25nic0]:9123",
                    "http://[::1]:9123",
                ],
            )

    def test_specific_bind_and_explicit_proxy_do_not_enumerate(self):
        with patch(
            "analysis.transfers.psutil.net_if_addrs",
            side_effect=AssertionError("No enumeration"),
        ):
            self.assertEqual(
                http_base_urls("192.0.2.1", 8123), ["http://192.0.2.1:8123"]
            )
            self.assertEqual(http_base_urls("::1", 8123), ["http://[::1]:8123"])
            self.assertEqual(
                http_base_urls("0.0.0.0", 8123, "https://proxy.example/data/"),
                ["https://proxy.example/data"],
            )


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = Path(temporary.name)
        self.manager = TaskManager(self.storage)
        self.task_id = self.manager.create("Analyze my uploaded file")["task_id"]
        self.server = create_server(self.manager)

    async def call(self, name, **arguments):
        blocks = await self.server.call_tool(name, arguments)
        block = blocks[0]
        assert isinstance(block, TextContent)
        return json.loads(block.text)

    async def test_mcp_multichunk_transfer_and_server_recreation(self):
        data = bytes(range(256)) * 8192 + b"tail"
        offset = 0
        while offset < len(data):
            end = min(offset + CHUNK_BYTES, len(data))
            result = await self.call(
                "upload_input",
                task_id=self.task_id,
                input_name="data.bin",
                data_base64=base64.b64encode(data[offset:end]).decode("ascii"),
                offset=offset,
                final=end == len(data),
            )
            self.assertTrue(result["ok"], result)
            offset = result["next_offset"]
            if not result["complete"]:
                self.assertFalse(self.manager.files(self.task_id, "inputs")["files"])
                self.manager = TaskManager(self.storage)
                self.server = create_server(self.manager)
        output = self.manager.file(self.manager.task(self.task_id), "outputs/data.bin")
        output.write_bytes(data)
        chunks, offset = [], 0
        while True:
            result = await self.call(
                "download_file",
                task_id=self.task_id,
                path="outputs/data.bin",
                offset=offset,
                limit=CHUNK_BYTES,
            )
            self.assertTrue(result["ok"], result)
            chunks.append(base64.b64decode(result["data"], validate=True))
            offset = result["next_offset"]
            if not result["has_more"]:
                break
        self.assertEqual(b"".join(chunks), data)

    async def test_http_defaults_and_preserved_mcp_fallback(self):
        catalog = {tool.name for tool in await self.server.list_tools()}
        self.assertTrue(
            {"upload_input", "download_file", "create_upload", "result_link"} <= catalog
        )
        self.assertNotIn("import_input", catalog)
        info = await self.call("analysis_info")
        self.assertEqual(info["data_transfer"]["preferred"], "http")
        self.assertTrue(info["data_transfer"]["http_available"])
        self.assertEqual(
            info["data_transfer"]["http_base_url"], "http://127.0.0.1:8001"
        )
        self.assertEqual(info["data_transfer"]["warnings"], [])
        upload = await self.call(
            "create_upload", task_id=self.task_id, input_name="data.bin"
        )
        self.assertTrue(upload["url"].startswith("http://127.0.0.1:8001/files/"))
        self.assertIn("warn", upload["next_action"])
        self.assertIn("upload_input", upload["next_action"])
        uploaded = await self.call(
            "upload_input",
            task_id=self.task_id,
            input_name="fallback.bin",
            data_base64="eA==",
        )
        self.assertTrue(uploaded["ok"])
        downloaded = await self.call(
            "download_file",
            task_id=self.task_id,
            path="inputs/fallback.bin",
        )
        self.assertEqual(base64.b64decode(downloaded["data"]), b"x")
        server = create_server(self.manager, port=9123, host="192.0.2.1")
        blocks = await server.call_tool("analysis_info", {})
        block = blocks[0]
        assert isinstance(block, TextContent)
        self.assertEqual(
            json.loads(block.text)["data_transfer"]["http_base_url"],
            "http://192.0.2.1:9123",
        )

    async def test_wildcard_urls_share_one_transfer_grant(self):
        bases = ["http://192.0.2.1:8001", "http://127.0.0.1:8001"]
        with patch("analysis.server.http_base_urls", return_value=bases):
            self.server = create_server(self.manager, host="0.0.0.0")
        info = await self.call("analysis_info")
        self.assertEqual(info["data_transfer"]["http_base_urls"], bases)
        upload = await self.call(
            "create_upload", task_id=self.task_id, input_name="data.bin"
        )
        token = upload["url"].rsplit("/", 1)[1]
        self.assertEqual(upload["urls"], [base + "/files/" + token for base in bases])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.sse_app()),
        ) as client:
            response = await client.put(upload["urls"][1], content=b"data")
            self.assertEqual(response.status_code, 200)
            response = await client.put(upload["urls"][0], content=b"replace")
            self.assertEqual(response.status_code, 400)
            await self.call(
                "write_file",
                task_id=self.task_id,
                path="outputs/data.txt",
                content="result",
            )
            download = await self.call(
                "result_link", task_id=self.task_id, path="outputs/data.txt"
            )
            self.assertEqual(len(download["urls"]), len(bases))
            for url in download["urls"]:
                response = await client.get(url)
                self.assertEqual(response.content, b"result")

    async def test_no_addresses_warns_without_blocking_mcp(self):
        with (
            patch("analysis.transfers.psutil.net_if_addrs", return_value={}),
            patch("analysis.transfers.psutil.net_if_stats", return_value={}),
        ):
            self.server = create_server(self.manager, host="0.0.0.0")
        info = await self.call("analysis_info")
        self.assertFalse(info["data_transfer"]["http_available"])
        self.assertEqual(info["data_transfer"]["warnings"][0]["fallback"], "mcp")
        result = await self.call(
            "create_upload", task_id=self.task_id, input_name="data.bin"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["fallback_tool"], "upload_input")
        result = await self.call(
            "upload_input",
            task_id=self.task_id,
            input_name="data.bin",
            data_base64="eA==",
        )
        self.assertTrue(result["ok"])

    async def test_invalid_chunks_and_overwrite_rejected(self):
        for encoded in (
            "invalid!",
            base64.b64encode(b"x" * (CHUNK_BYTES + 1)).decode(),
        ):
            result = await self.call(
                "upload_input",
                task_id=self.task_id,
                input_name="data.bin",
                data_base64=encoded,
            )
            self.assertFalse(result["ok"])
        result = await self.call(
            "upload_input",
            task_id=self.task_id,
            input_name="data.bin",
            data_base64="eA==",
        )
        self.assertTrue(result["ok"])
        result = await self.call(
            "upload_input",
            task_id=self.task_id,
            input_name="data.bin",
            data_base64="eQ==",
        )
        self.assertFalse(result["ok"])
        result = await self.call(
            "download_file",
            task_id=self.task_id,
            path="../task.json",
        )
        self.assertFalse(result["ok"])

    def http_client(self):
        self.server = create_server(self.manager, public_url="http://test")
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.sse_app()),
            base_url="http://test",
        )

    async def test_http_raw_upload_download_and_restart(self):
        data = bytes(range(256)) * 8192 + b"tail"
        async with self.http_client() as client:
            info = await self.call("analysis_info")
            self.assertTrue(info["data_transfer"]["http_available"])
            self.assertEqual(info["data_transfer"]["warnings"], [])
            upload = await self.call(
                "create_upload",
                task_id=self.task_id,
                input_name="data.bin",
            )
            self.assertEqual(upload["method"], "PUT")
            response = await client.get(upload["url"])
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response.json()["error"], "method_not_allowed")
            response = await client.put(upload["url"], content=data)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["bytes"], len(data))
            self.assertEqual(
                (await client.put(upload["url"], content=b"x")).status_code, 400
            )
            task = self.manager.task(self.task_id)
            self.assertEqual((task / "workspace/inputs/data.bin").read_bytes(), data)
            (task / "workspace/outputs/data.bin").write_bytes(data)
            download = await self.call(
                "result_link",
                task_id=self.task_id,
                path="outputs/data.bin",
            )
            self.assertEqual(download["method"], "GET")
        self.manager = TaskManager(self.storage)
        async with self.http_client() as client:
            for _ in range(2):
                response = await client.get(download["url"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, data)
            response = await client.put(download["url"], content=b"x")
            self.assertEqual(response.status_code, 405)
            # HTTP upload can be read back through the preserved MCP path.
            result = await self.call(
                "download_file",
                task_id=self.task_id,
                path="inputs/data.bin",
                limit=256,
            )
            self.assertEqual(base64.b64decode(result["data"]), data[:256])

    async def test_http_expiry_cancellation_and_boundaries(self):
        async with self.http_client() as client:
            for name in ("../secret", "/etc/passwd"):
                result = await self.call(
                    "create_upload",
                    task_id=self.task_id,
                    input_name=name,
                )
                self.assertFalse(result["ok"])
            result = await self.call(
                "result_link",
                task_id=self.task_id,
                path="work/code.py",
            )
            self.assertFalse(result["ok"])
            upload = await self.call(
                "create_upload",
                task_id=self.task_id,
                input_name="expired.bin",
            )
            token = upload["url"].rsplit("/", 1)[1]
            grant = self.storage / ".transfers" / (token + ".json")
            record = read_json(grant)
            record["expires_at"] = time.time() - 1
            write_json(grant, record)
            response = await client.put(upload["url"], content=b"x")
            self.assertEqual(response.status_code, 400)
            self.assertIn("expired", response.json()["error"])
            upload = await self.call(
                "create_upload",
                task_id=self.task_id,
                input_name="cancelled.bin",
            )
            self.manager.cancel_task(self.task_id)
            response = await client.put(upload["url"], content=b"x")
            self.assertEqual(response.status_code, 400)
            self.assertIn("cancelled", response.json()["error"])
            response = await client.get("/files/not-a-token")
            self.assertEqual(response.status_code, 400)
            response = await client.get("/files/" + "a" * 43)
            self.assertEqual(response.status_code, 404)
            self.assertFalse(
                list((self.manager.task(self.task_id) / "uploads").iterdir())
            )

    async def test_http_failure_cleanup_and_mcp_fallback(self):
        async with self.http_client() as client:
            upload = await self.call(
                "create_upload",
                task_id=self.task_id,
                input_name="data.bin",
            )

            async def interrupted():
                yield b"incomplete"
                with self.assertRaises(ValueError):
                    self.manager.execute(self.task_id, "true")
                raise RuntimeError("client disconnected")

            with self.assertRaisesRegex(RuntimeError, "client disconnected"):
                await client.put(upload["url"], content=interrupted())
            task = self.manager.task(self.task_id)
            self.assertFalse(list((task / "uploads").iterdir()))
            self.assertFalse(self.manager.files(self.task_id, "inputs")["files"])
            result = await self.call(
                "upload_input",
                task_id=self.task_id,
                input_name="data.bin",
                data_base64="eA==",
            )
            self.assertTrue(result["ok"])
            response = await client.put(upload["url"], content=b"replace")
            self.assertEqual(response.status_code, 400)
            self.assertEqual((task / "workspace/inputs/data.bin").read_bytes(), b"x")

    async def test_https_base_url_and_invalid_urls(self):
        self.server = create_server(
            self.manager, public_url="https://files.example/analysis"
        )
        upload = await self.call(
            "create_upload",
            task_id=self.task_id,
            input_name="data.bin",
        )
        self.assertTrue(
            upload["url"].startswith("https://files.example/analysis/files/")
        )
        for url in (
            "file:///etc",
            "ftp://host",
            "https://user:password@host",
            "https://host/?token=x",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                create_server(self.manager, public_url=url)


if __name__ == "__main__":
    unittest.main()
