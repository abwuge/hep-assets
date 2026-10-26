"""Task access boundaries and opt-in real macOS execution isolation."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from ams_flux_server.task_server import TaskWorkspace, create_task_server


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.task = (root / "task").resolve()
        self.task.mkdir()
        (self.task / "Data").mkdir()
        (self.task / "TASK.md").write_text("immutable task")
        self.project = Path(__file__).resolve().parents[1]
        self.workspace = TaskWorkspace(self.task, self.project / ".runtime/ams-flux")
        self.addCleanup(self.workspace.close)

    def test_paths_and_supplied_inputs(self):
        for path in ("../secret", "/etc/passwd"):
            with self.assertRaises(ValueError):
                self.workspace.path(path)
        for path in (".", "TASK.md", "Data/input.root", "Method_Reference/script.py"):
            with self.assertRaises(ValueError):
                self.workspace.path(path, write=True)
        self.assertEqual(self.workspace.path("submission/run.py", write=True),
                         self.task / "submission/run.py")

    def test_symlinks_cannot_escape_or_overwrite_inputs(self):
        (self.task / "outside").symlink_to(self.project)
        (self.task / "input_link").symlink_to(self.task / "Data", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.workspace.path("outside/demo_agent_task/submission/run.py")
        with self.assertRaises(ValueError):
            self.workspace.path("input_link/input.root", write=True)

    def test_read_file_makes_truncation_actionable(self):
        server = create_task_server(self.workspace)
        blocks = asyncio.run(server.call_tool("read_file", {"path": "TASK.md", "limit": 4}))
        result = json.loads(blocks[0].text)
        self.assertEqual(result["text"], "immu")
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_offset"], 4)
        self.assertIn("offset=4", result["next_action"])





    def test_catalog_matches_recorded_baseline(self):
        recorded = json.loads((self.project / "runs/codex-baseline/discovered-tools.json").read_text())
        expected = [{**tool, "name": tool["name"].removeprefix("AMSTask.")} for tool in recorded]
        actual = [tool.model_dump() for tool in asyncio.run(create_task_server(self.workspace).list_tools())]
        self.assertEqual(actual, expected)

    @unittest.skipUnless(os.environ.get("AMS_TASK_SANDBOX") == "1", "Opt in to macOS sandbox execution")
    def test_close_stops_command_process_group(self):
        result = self.workspace.execute("sleep 600 & wait")
        self.assertEqual(result["status"], "running", result)
        process = self.workspace.processes[result["command_id"]]
        self.workspace.close()
        self.assertIsNotNone(process.poll())
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)

    @unittest.skipUnless(os.environ.get("AMS_TASK_SANDBOX") == "1", "Opt in to macOS sandbox execution")
    def test_real_runtime_and_isolation(self):
        reference = self.project / "demo_agent_task/submission/run.py"
        credential = Path.home() / ".claude/settings.json"
        code = f'''
import os,socket,ROOT
from pathlib import Path
assert ROOT.gROOT.GetVersion() == '6.34.10'
Path('submission').mkdir()
Path('submission/result.txt').write_text('ok')
for path in {list(map(str, (reference, credential)))!r}:
    try: open(path).close()
    except PermissionError: pass
    else: raise AssertionError('Sensitive file readable: '+path)
try: open('TASK.md','a').close()
except PermissionError: pass
else: raise AssertionError('Input writable')
try: socket.socket().connect(('127.0.0.1',1))
except PermissionError: pass
else: raise AssertionError('Network enabled')
assert not any('KEY' in key or 'TOKEN' in key for key in os.environ)
print('PASS: ROOT, writes, immutable inputs, reference/credential denial, network denial')
'''
        result = self.workspace.execute("python - <<'PY'\n" + code + "\nPY")
        while result["status"] == "running":
            result = self.workspace.poll(result["command_id"], 10)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("PASS:", result["output"])


if __name__ == "__main__":
    unittest.main()
