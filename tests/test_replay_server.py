"""Read-only replay discovery, live updates and local access boundaries."""

import json
from pathlib import Path
import tempfile
import unittest

from ams_flux_server.replay_server import create_app
from ams_flux_server.trace_viewer import load_trial, render_html, content_text


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = create_app(self.root).test_client()

    def trial(self, name="trial", started=1):
        directory = self.root / name
        directory.mkdir()
        (directory / "trial.json").write_text(json.dumps({"model": "Agents-A1", "started_at": started}))
        return directory

    @staticmethod
    def call():
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "call-1", "name": "execute", "input": {"command": "python run.py"}}
        ]}}

    def test_empty_and_discovery_without_restart(self):
        self.assertEqual(self.client.get("/api/replay").json, {"trials": []})
        self.trial("new-trial")
        response = self.client.get("/api/replay")
        self.assertEqual(response.json["trials"][0]["name"], "new-trial")
        self.assertEqual(response.json["trials"][0]["items"], [])

    def test_append_completes_partial_event_and_changes_etag(self):
        directory = self.trial()
        path = directory / "claude.jsonl"
        line = json.dumps(self.call())
        path.write_text(line[:20])
        first = self.client.get("/api/replay")
        self.assertEqual(first.json["trials"][0]["items"], [])
        self.assertEqual(self.client.get("/api/replay", headers={"If-None-Match": first.headers["ETag"]}).status_code, 304)
        with path.open("a") as stream:
            stream.write(line[20:] + "\n")
        second = self.client.get("/api/replay", headers={"If-None-Match": first.headers["ETag"]})
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])
        self.assertEqual(second.json["trials"][0]["items"][0]["label"], "execute")
        result = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call-1",
                  "content": [{"type": "text", "text": json.dumps({"status": "finished", "exit_code": -5, "output": "crash"})}]}]}}
        with path.open("a") as stream:
            stream.write(json.dumps(result) + "\n")
        item = self.client.get("/api/replay").json["trials"][0]["items"][0]
        self.assertTrue(item["error"])
        self.assertIn("crash", item["result"])

    def test_assessment_update_and_deleted_trial(self):
        directory = self.trial()
        initial = self.client.get("/api/replay")
        (directory / "assessment.json").write_text('{"completion":"incomplete"}')
        updated = self.client.get("/api/replay")
        self.assertNotEqual(initial.headers["ETag"], updated.headers["ETag"])
        self.assertEqual(updated.json["trials"][0]["findings"]["completion"], "incomplete")
        (directory / "trial.json").unlink()
        self.assertEqual(self.client.get("/api/replay").json["trials"], [])

    def test_readonly_and_same_origin(self):
        self.assertEqual(self.client.post("/api/replay").status_code, 405)
        self.assertEqual(self.client.get("/api/replay", base_url="http://example.com").status_code, 403)
        self.assertEqual(self.client.get("/api/replay", headers={"Origin": "https://example.com"}).status_code, 403)
        self.assertEqual(self.client.get("/api/replay", headers={"Origin": "http://localhost"}).status_code, 200)
        self.assertEqual(self.client.get("/trial.json").status_code, 404)
        self.assertEqual(self.client.get("/../pyproject.toml").status_code, 404)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_symlink_records_are_not_read(self):
        directory = self.trial()
        outside = self.root / "outside.txt"
        outside.write_text("private")
        (directory / "prompt.txt").symlink_to(outside)
        response = self.client.get("/api/replay")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)

    def test_half_written_metadata_recovers(self):
        directory = self.trial()
        (directory / "trial.json").write_text('{"model":')
        self.assertEqual(self.client.get("/api/replay").status_code, 503)
        (directory / "trial.json").write_text('{"model":"Agents-A1"}')
        self.assertEqual(self.client.get("/api/replay").status_code, 200)

    def test_offline_contract_and_script_escape(self):
        directory = self.trial()
        (directory / "prompt.txt").write_text('</script><script>alert("injection")</script> __LIVE__')
        html = render_html([load_trial(directory)])
        self.assertIn("const live=false", html)
        self.assertNotIn('</script><script>alert("injection")', html)
        self.assertIn("\\u003c/script>", html)
        self.assertIn(" __LIVE__", html)



if __name__ == "__main__":
    unittest.main()
