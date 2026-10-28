"""Native sandbox command execution, recovery and cancellation with synthetic inputs."""

import base64
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis import execution
from analysis.tasks import TaskManager


class SandboxTests(unittest.TestCase):
    def test_linux_mounts_and_namespace_policy(self):
        workspace = Path("/srv/tasks/one/workspace")
        assets = Path("/srv/assets")
        with (
            patch.object(sys, "platform", "linux"),
            patch("analysis.execution.shutil.which", return_value="/usr/bin/bwrap"),
        ):
            args = execution.sandbox_command(
                Path("/srv/tasks/one/commands/id"),
                workspace,
                "echo 'a b'",
                assets,
            )
        self.assertEqual(args[0], "/usr/bin/bwrap")
        for flag in ("--unshare-all", "--die-with-parent", "--new-session"):
            self.assertIn(flag, args)
        mounts = [
            args[index + 1 : index + 3]
            for index, value in enumerate(args)
            if value == "--ro-bind"
        ]
        self.assertIn([str(workspace), str(workspace)], mounts)
        self.assertIn([str(assets), str(assets)], mounts)
        self.assertNotIn(["/", "/"], mounts)
        self.assertNotIn(["/etc", "/etc"], mounts)
        writable = [
            args[index + 1 : index + 3]
            for index, value in enumerate(args)
            if value == "--bind"
        ]
        self.assertEqual(
            writable,
            [
                [str(workspace / name)] * 2
                for name in ("work", "outputs", ".home", ".tmp")
            ],
        )
        self.assertEqual(
            args[-8:],
            [
                "--chdir",
                str(workspace),
                "--remount-ro",
                "/",
                "--",
                "/bin/bash",
                "-c",
                "echo 'a b'",
            ],
        )

    def test_missing_backend_and_unsupported_platform_fail_closed(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch("analysis.execution.shutil.which", return_value=None),
            self.assertRaisesRegex(ValueError, "Install bubblewrap"),
        ):
            execution.check_sandbox()
        with (
            patch.object(sys, "platform", "freebsd"),
            self.assertRaisesRegex(ValueError, "supports Linux and macOS"),
        ):
            execution.check_sandbox()

    def test_startup_surfaces_kernel_denial(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch("analysis.execution.shutil.which", return_value="/usr/bin/bwrap"),
            patch("analysis.execution.subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess(
                [],
                1,
                "",
                "bwrap: Creating new namespace failed: Operation not permitted",
            )
            with self.assertRaisesRegex(ValueError, "Operation not permitted"):
                execution.check_sandbox()
            self.assertEqual(run.call_count, 1)


@unittest.skipUnless(sys.platform in {"linux", "darwin"}, "Requires Linux or macOS")
class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manager = TaskManager(self.root / "tasks")
        self.task_id = self.manager.create("Analyze supplied values")["task_id"]
        self.addCleanup(self.manager.cancel_task, self.task_id)

    def wait(self, command_id: str) -> dict:
        directory = self.manager.command(self.task_id, command_id)
        for _ in range(20):
            result = execution.poll(directory, 1)
            if result["status"] in execution.TERMINAL:
                return result
        self.fail("Command did not finish within 20 seconds")

    def test_runtime_isolation_and_output(self):
        self.manager.upload_input(
            self.task_id, "data.txt", base64.b64encode(b"data").decode()
        )
        other = self.manager.create("Other user's data")["task_id"]
        self.manager.write_file(other, "work/secret.txt", "not readable")
        secret = self.manager.file(self.manager.task(other), "work/secret.txt")
        code = f"""
import errno, os, socket
from pathlib import Path
assert 'HEP_TEST_SECRET' not in os.environ
assert Path('inputs/data.txt').read_text() == 'data'
Path('outputs/result.txt').write_text('computed')
try: os.link('inputs/data.txt', 'work/input-hardlink')
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS, errno.EXDEV), error
else:
    try: Path('work/input-hardlink').write_text('bad')
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error
    else: raise AssertionError('input writable through hard link')
for path in ['inputs/data.txt', '../outside']:
    try: Path(path).write_text('bad')
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error
    else: raise AssertionError('write allowed: ' + path)
try: Path({str(secret)!r}).read_text()
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES, errno.ENOENT), error
else: raise AssertionError('other task readable')
try: socket.socket().connect(('127.0.0.1', 1))
except OSError as error:
    assert error.errno in (
        errno.EPERM, errno.EACCES, errno.ENETUNREACH, errno.ECONNREFUSED
    ), error
else: raise AssertionError('network allowed')
print('verified')
"""
        self.manager.write_file(self.task_id, "work/check.py", code)
        with patch.dict(os.environ, {"HEP_TEST_SECRET": "not inherited"}):
            command = self.manager.execute(
                self.task_id, "/usr/bin/python3 work/check.py"
            )
        result = self.wait(command["command_id"])
        self.assertEqual(result["status"], "succeeded", result)
        self.assertIn("verified", result["output"])

    def test_shell_needs_no_scientific_environment(self):
        command = self.manager.execute(self.task_id, "printf ordinary-command")
        result = self.wait(command["command_id"])
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["output"], "ordinary-command")
        record = execution.read_json(
            self.manager.command(self.task_id, command["command_id"]) / "command.json"
        )
        self.assertNotIn("runtime", record)

    def test_executable_asset_is_read_only(self):
        software = self.root / "software"
        (software / "demo").mkdir(parents=True)
        (software / "demo/run.sh").write_text("printf demo-program")
        self.manager = TaskManager(self.root / "tasks", assets=software)
        command = self.manager.execute(self.task_id, 'sh "$ASSETS_DIR/demo/run.sh"')
        result = self.wait(command["command_id"])
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["output"], "demo-program")
        command = self.manager.execute(
            self.task_id, 'printf bad > "$ASSETS_DIR/demo/run.sh"'
        )
        result = self.wait(command["command_id"])
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual((software / "demo/run.sh").read_text(), "printf demo-program")

    def test_restore_running_command_and_busy_guard(self):
        command = self.manager.execute(self.task_id, "sleep 2; printf recovered")
        with self.assertRaises(ValueError):
            self.manager.execute(self.task_id, "true")
        restored = TaskManager(self.manager.storage)
        self.assertEqual(restored.info(self.task_id)["status"], "running")
        self.manager = restored
        result = self.wait(command["command_id"])
        self.assertEqual(result["output"], "recovered", result)

    def test_failure_timeout_and_continuation(self):
        command = self.manager.execute(self.task_id, "printf bad; exit 7")
        result = self.wait(command["command_id"])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(self.manager.info(self.task_id)["status"], "needs_attention")
        self.manager.write_file(self.task_id, "outputs/report.md", "not yet verified")
        with self.assertRaises(ValueError):
            self.manager.finish(self.task_id, "Done", ["outputs/report.md"])
        command = self.manager.execute(self.task_id, "sleep 600", timeout_seconds=1)
        result = self.wait(command["command_id"])
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("time limit", result["error"])
        command = self.manager.execute(self.task_id, "printf repaired")
        self.assertEqual(self.wait(command["command_id"])["status"], "succeeded")
        self.manager.finish(self.task_id, "Verified", ["outputs/report.md"])

    def test_cancellation_stops_descendants(self):
        # A descendant writes after a delay unless cancellation kills the whole group.
        command = self.manager.execute(
            self.task_id,
            "(sleep 2; printf leaked > outputs/leak.txt) & wait",
        )
        result = execution.cancel(
            self.manager.command(self.task_id, command["command_id"])
        )
        self.assertEqual(result["status"], "cancelled", result)
        self.manager.cancel_task(self.task_id)
        self.assertEqual(self.manager.info(self.task_id)["status"], "cancelled")
        check = self.manager.execute(
            self.task_id, "sleep 2; test ! -e outputs/leak.txt"
        )
        self.assertEqual(self.wait(check["command_id"])["status"], "succeeded")
        # Network denial is established against a listener that is reachable outside.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            probe = [
                "/usr/bin/python3",
                "-c",
                f"import socket; socket.create_connection(('127.0.0.1', {port}), 1)",
            ]
            subprocess.run(probe, check=True, capture_output=True)
            command = self.manager.execute(self.task_id, shlex.join(probe))
            self.assertEqual(self.wait(command["command_id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main()
