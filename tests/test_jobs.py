"""Job lifecycle tests using small subprocesses, without ROOT or the SDK."""

import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ams_flux_server.jobs import JobManager
from ams_flux_server.storage import read_json, write_json


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.source = self.project / "demo_agent_task/submission"
        self.source.mkdir(parents=True)
        runtime = self.project / ".runtime/ams-flux/bin"
        runtime.mkdir(parents=True)
        (runtime / "python").symlink_to(sys.executable)
        (runtime / "root").touch()
        (self.source / "Data").mkdir()
        (self.source / "Data/input.root").write_bytes(b"input")
        (self.source / "01_Flux_Formula").mkdir()
        (self.source / "01_Flux_Formula/results.root").write_bytes(b"old result")
        (self.source / "run_records").mkdir()
        (self.source / "run_records/old.log").touch()
        self.manager = JobManager(self.project)

    def wait(self, job_id, condition=lambda state: state["status"] in {"failed", "succeeded", "cancelled"}):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.manager.status(job_id)
            if condition(state):
                return state
            time.sleep(.03)
        self.manager.cancel(job_id)
        self.fail(f"Job did not reach the expected state: {state}")

    def test_failure_and_clean_workspace(self):
        (self.source / "run.py").write_text("print('fixture failure', flush=True)\nraise SystemExit(7)\n")
        job = self.manager.run_analysis()["job_id"]
        state = self.wait(job)
        self.assertEqual((state["status"], state["exit_code"]), ("failed", 7))
        self.assertIn("fixture failure", state["logs"]["process.log"])
        submission = self.manager.directory(job) / "submission"
        self.assertTrue((submission / "Data/input.root").exists())
        self.assertFalse((submission / "01_Flux_Formula/results.root").exists())
        self.assertFalse((submission / "run_records/old.log").exists())
        self.assertEqual(JobManager(self.project).status(job)["status"], "failed")
        with self.assertRaisesRegex(ValueError, "requires a succeeded"):
            self.manager.validate(job)
        with self.assertRaisesRegex(ValueError, "require a succeeded"):
            self.manager.result(job, "spectrum")

    def test_cancel_process_group_and_busy(self):
        child = ("import signal,time,pathlib,sys; "
                 "signal.signal(signal.SIGTERM, lambda *_: "
                 "(pathlib.Path('child_stopped').touch(), sys.exit(0))); "
                 "pathlib.Path('child_ready').touch(); time.sleep(60)")
        (self.source / "run.py").write_text(
            "import subprocess,sys\n"
            f"subprocess.run([sys.executable, '-c', {child!r}])\n"
        )
        job = self.manager.run_analysis()["job_id"]
        self.addCleanup(self.manager.cancel, job)
        directory = self.manager.directory(job)
        self.wait(job, lambda state: (directory / "submission/child_ready").exists())
        with self.assertRaisesRegex(ValueError, "busy"):
            self.manager.run_analysis()
        state = JobManager(self.project).cancel(job)
        self.assertEqual(state["status"], "cancelled")
        self.assertTrue((directory / "submission/child_stopped").exists())
        self.assertFalse(self.manager.worker_alive(directory))

    def test_invalid_identifiers_and_missing_runtime(self):
        for job_id in ("../anything", "/tmp", "a" * 32):
            with self.assertRaises(ValueError):
                self.manager.status(job_id)
        (self.project / ".runtime/ams-flux/bin/root").unlink()
        with self.assertRaisesRegex(ValueError, "runtime is missing"):
            self.manager.run_analysis()

    def test_cancel_stops_descendants_that_ignore_termination(self):
        child = ("import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 "pathlib.Path('child_ready').touch(); "
                 "exec(\"while True:\\n pathlib.Path('heartbeat').touch(); time.sleep(.02)\")")
        (self.source / "run.py").write_text(
            "import subprocess,sys\n"
            f"subprocess.run([sys.executable, '-c', {child!r}])\n"
        )
        job = self.manager.run_analysis()["job_id"]
        self.addCleanup(self.manager.cancel, job)
        directory = self.manager.directory(job)
        self.wait(job, lambda state: (directory / "submission/child_ready").exists())
        self.assertEqual(self.manager.cancel(job)["status"], "cancelled")
        heartbeat = directory / "submission/heartbeat"
        time.sleep(.05)
        modified = heartbeat.stat().st_mtime_ns
        time.sleep(.1)
        self.assertEqual(heartbeat.stat().st_mtime_ns, modified)

    def test_interrupted_worker_is_not_successful(self):
        job = "a" * 32
        directory = self.manager.runs / job
        directory.mkdir()
        write_json(directory / "job.json", {"kind": "analysis", "created_at": "fixture",
                                            "submission": str(directory)})
        write_json(directory / "state.json", {"status": "running", "pid": os.getpid()})
        self.assertEqual(self.manager.status(job)["status"], "failed")

    def test_cancel_forcefully_stops_an_unresponsive_child(self):
        (self.source / "run.py").write_text(
            "import signal,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path('child_ready').touch()\ntime.sleep(60)\n"
        )
        job = self.manager.run_analysis()["job_id"]
        self.addCleanup(self.manager.cancel, job)
        directory = self.manager.directory(job)
        self.wait(job, lambda state: (directory / "submission/child_ready").exists())
        state = self.manager.cancel(job)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["exit_code"], -signal.SIGKILL)
        self.assertFalse(self.manager.worker_alive(directory))

    def test_completion_during_status_read_is_preserved(self):
        job = "b" * 32
        directory = self.manager.runs / job
        directory.mkdir()
        write_json(directory / "job.json", {"kind": "analysis", "created_at": "fixture",
                                            "submission": str(directory)})
        write_json(directory / "state.json", {"status": "running"})

        def finish(_):
            write_json(directory / "state.json", {"status": "succeeded", "exit_code": 0})
            return False

        with patch.object(self.manager, "worker_alive", finish):
            self.assertEqual(self.manager.status(job)["status"], "succeeded")
        self.assertEqual(read_json(directory / "state.json")["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
