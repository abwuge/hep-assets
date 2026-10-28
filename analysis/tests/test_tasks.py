"""Input transfer, task boundaries and result delivery without scientific packages."""

import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scp.types import ImageContent, TextContent

from analysis import server
from analysis.server import create_server
from analysis.tasks import TaskManager

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
    "AwMCAO+aD1sAAAAASUVORK5CYII="
)


class StartupTests(unittest.TestCase):
    def test_default_registers_with_local_hub(self):
        with (
            patch.object(sys, "argv", ["analysis"]),
            patch.object(server, "TaskManager"),
            patch.object(server.execution, "check_sandbox"),
            patch.object(server, "create_server", return_value=Mock()) as create,
            patch.object(server.threading, "Thread") as thread,
            patch.object(server.socket, "create_connection"),
            patch.object(server.httpx, "Client") as http,
        ):

            def run(**kwargs):
                thread.call_args.kwargs["target"]()

            create.return_value.run.side_effect = run
            server.main()
            thread.return_value.start.assert_called_once()
            client = http.return_value.__enter__.return_value
            self.assertEqual(
                client.post.call_args.args[0],
                "http://127.0.0.1:46380/register_server",
            )


class TaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manager = TaskManager(self.root / "tasks")
        self.task_id = self.manager.create("Analyze my data", "Analysis")["task_id"]

    def test_create_task_advertises_assets_and_client_http_transfer(self):
        assets = self.root / "assets"
        assets.mkdir()
        manager = TaskManager(self.root / "tasks", assets=assets)
        service = create_server(manager)
        blocks = asyncio.run(service.call_tool("create_task", {"request": "Analyze"}))
        block = blocks[0]
        assert isinstance(block, TextContent)
        result = json.loads(block.text)
        self.assertTrue(result["ok"])
        self.assertEqual(result["assets"]["path"], "assets/")
        self.assertIn("list_files", result["assets"]["next_action"])
        self.assertIn("does not upload", result["paths"])
        self.assertIn("paths may differ", result["paths"])
        self.assertEqual(result["data_transfer"]["preferred"], "http")
        self.assertIn("create_upload", result["next_action"])
        self.assertIn("HTTP PUT", result["next_action"])
        self.assertFalse(manager.files(result["task_id"], "inputs")["files"])
        blocks = asyncio.run(
            service.call_tool("get_task", {"task_id": result["task_id"]})
        )
        block = blocks[0]
        assert isinstance(block, TextContent)
        restored = json.loads(block.text)
        self.assertEqual(restored["assets"], str(assets))
        self.assertEqual(restored["workspace_context"]["assets"], result["assets"])
        self.assertEqual(
            restored["workspace_context"]["data_transfer"], result["data_transfer"]
        )

    def test_create_task_reports_no_shared_assets(self):
        service = create_server(self.manager)
        blocks = asyncio.run(service.call_tool("create_task", {"request": "Analyze"}))
        block = blocks[0]
        assert isinstance(block, TextContent)
        result = json.loads(block.text)
        self.assertFalse(result["assets"]["available"])
        self.assertIsNone(result["assets"]["path"])

    def test_server_paths_are_not_input_sources(self):
        self.assertFalse(hasattr(self.manager, "import_input"))
        server = create_server(self.manager)
        catalog = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertNotIn("import_input", catalog)
        outside = self.root / "secret"
        outside.write_text("private")
        for path in (str(outside), "../secret"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.manager.file(self.manager.task(self.task_id), path)

    def test_upload_resume_and_binary_download(self):
        data = bytes(range(256)) * 300
        first = self.manager.upload_input(
            self.task_id,
            "data.bin",
            base64.b64encode(data[:40000]).decode(),
            final=False,
        )
        self.assertEqual(first["next_offset"], 40000)
        with self.assertRaises(ValueError):
            self.manager.upload_input(self.task_id, "data.bin", "", offset=1)
        with self.assertRaises(ValueError):
            self.manager.execute(self.task_id, "true")
        restored = TaskManager(self.manager.storage)
        self.assertEqual(
            restored.info(self.task_id)["uploads"][0]["next_offset"], 40000
        )
        restored.upload_input(
            self.task_id, "data.bin", base64.b64encode(data[40000:]).decode(), 40000
        )
        chunks, offset = [], 0
        while True:
            result = restored.download(self.task_id, "inputs/data.bin", offset, 10000)
            chunks.append(base64.b64decode(result["data"]))
            offset = result["next_offset"]
            if not result["has_more"]:
                break
        self.assertEqual(b"".join(chunks), data)

    def test_paths_and_input_immutability(self):
        task = self.manager.task(self.task_id)
        (task / "workspace/work/escape").symlink_to(self.root)
        for path in ("../task.json", "/etc/passwd", "work/escape/secret", ".home/key"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.manager.file(task, path)
        with self.assertRaises(ValueError):
            self.manager.write_file(self.task_id, "inputs/data.csv", "overwritten")
        other = self.manager.create("Other task")["task_id"]
        with self.assertRaises(ValueError):
            self.manager.file(task, f"../../{other}/workspace/inputs/data.csv")

    def test_shared_assets_are_read_only_and_confined(self):
        assets = self.root / "assets"
        (assets / "knowledge/demo_flux").mkdir(parents=True)
        (assets / "knowledge/demo_flux/method.md").write_text("public method")
        manager = TaskManager(self.root / "tasks", assets=assets)
        path = "assets/knowledge/demo_flux/method.md"
        self.assertEqual(
            manager.read_file(self.task_id, path, 0, 100)["text"], "public method"
        )
        self.assertEqual(
            manager.files(self.task_id, "assets")["files"][0]["path"], path
        )
        (assets / "method.md").symlink_to("knowledge/demo_flux/method.md")
        self.assertEqual(
            manager.read_file(self.task_id, "assets/method.md", 0, 100)["text"],
            "public method",
        )
        TaskManager(self.root / "tasks", assets=assets)
        with self.assertRaises(ValueError):
            manager.write_file(self.task_id, path, "changed")
        for path in ("assets/../secret", "assets/knowledge/../../../secret"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                manager.read_file(self.task_id, path, 0, 100)
        (assets / "escape").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            TaskManager(self.root / "tasks", assets=assets)
        with self.assertRaisesRegex(ValueError, "separate"):
            TaskManager(self.root / "tasks", assets=self.root)

    def test_completion_and_continuation(self):
        with self.assertRaises(ValueError):
            self.manager.finish(self.task_id, "Done", ["outputs/missing.txt"])
        self.manager.write_file(self.task_id, "outputs/report.md", "# Actual result")
        record = self.manager.finish(self.task_id, "Finding", ["outputs/report.md"])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(self.manager.list_tasks()["tasks"][0]["status"], "completed")
        self.manager.write_file(self.task_id, "work/followup.py", "print(1)")
        info = self.manager.info(self.task_id)
        self.assertEqual(info["status"], "working")
        self.assertNotIn("completion", info)
        self.assertTrue(info["recent_activity"])

    def test_mcp_error_envelope_and_native_image(self):
        server = create_server(self.manager)
        blocks = asyncio.run(
            server.call_tool(
                "read_file", {"task_id": self.task_id, "path": "../secret"}
            )
        )
        block = blocks[0]
        assert isinstance(block, TextContent)
        self.assertFalse(json.loads(block.text)["ok"])
        path = self.manager.file(self.manager.task(self.task_id), "outputs/preview.png")
        path.write_bytes(base64.b64decode(PNG))
        blocks = asyncio.run(
            server.call_tool(
                "preview_image",
                {"task_id": self.task_id, "path": "outputs/preview.png"},
            )
        )
        block = blocks[0]
        assert isinstance(block, ImageContent)
        self.assertEqual(block.data, PNG)


if __name__ == "__main__":
    unittest.main()
