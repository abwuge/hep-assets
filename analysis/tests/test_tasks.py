"""Input transfer, task boundaries and result delivery without scientific packages."""

import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

from scp.types import ImageContent, TextContent

from analysis.server import create_server
from analysis.tasks import TaskManager

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
    "AwMCAO+aD1sAAAAASUVORK5CYII="
)


class TaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manager = TaskManager(self.root / "tasks", Path(sys.base_prefix))
        self.task_id = self.manager.create("Analyze my data", "Analysis")["task_id"]

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
        restored = TaskManager(self.manager.storage, Path(sys.base_prefix))
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
