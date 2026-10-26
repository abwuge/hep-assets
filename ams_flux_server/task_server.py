"""Serve an independent task workspace, with macOS sandboxed execution."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid

from scp.server.fastmcp import FastMCP


class TaskWorkspace:
    def __init__(self, workspace, runtime, threads=2):
        self.workspace = Path(workspace).resolve()
        self.runtime = Path(runtime).resolve()
        if threads < 1:
            raise ValueError("threads must be positive")
        self.threads = threads
        self.records = self.workspace.parent / "commands"
        self.records.mkdir(exist_ok=True)
        self.processes = {}
        self.lock = threading.Lock()
        self.profile = self.records / "sandbox.sb"
        quote = json.dumps
        self.profile.write_text(
            '(version 1)\n(allow default)\n(deny network*)\n(deny signal)\n'
            '(deny process-info* (target others))\n'
            '(deny file-read-data (require-all (subpath "/Users") '
            f'(require-not (subpath {quote(str(self.workspace))})) '
            f'(require-not (subpath {quote(str(self.runtime))}))))\n'
            '(deny file-write* (require-all '
            f'(require-not (subpath {quote(str(self.workspace))})) '
            '(require-not (literal "/dev/null"))))\n'
            + ''.join(f'(deny file-write* (subpath {quote(str(self.workspace / name))}))\n'
                      for name in ("Data", "Method_Reference", "TASK.md", "README.md",
                                   "ACCEPTANCE.md", "Template_Fitting_Method.md",
                                   "Efficiency_Correction_Method.md", "MANIFEST.json", "verify_package.py"))
        )
        for name in (".tmp", ".home"):
            (self.workspace / name).mkdir(exist_ok=True)

    def path(self, name, write=False):
        path = (self.workspace / name).resolve()
        if not path.is_relative_to(self.workspace):
            raise ValueError("Use a relative path inside the task workspace")
        if write and (path == self.workspace or path.parts[len(self.workspace.parts)] in {
            "Data", "Method_Reference", "TASK.md", "README.md", "ACCEPTANCE.md",
            "Template_Fitting_Method.md", "Efficiency_Correction_Method.md",
            "MANIFEST.json", "verify_package.py",
        }):
            raise ValueError("Supplied task inputs are read-only; write to submission/")
        return path

    def execute(self, command):
        with self.lock:
            if any(p.poll() is None for p in self.processes.values()):
                raise ValueError("A command is running; poll or cancel it before starting another")
            command_id = uuid.uuid4().hex
            record = self.records / command_id
            record.mkdir()
            (record / "command.txt").write_text(command)
            (record / "status.json").write_text(json.dumps({"command_id": command_id, "status": "running", "started_at": time.time()}))
            environment = {
                "PATH": f"{self.runtime}/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(self.workspace / ".home"),
                "TMPDIR": str(self.workspace / ".tmp"), "LANG": "en_US.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                **{name: str(self.threads) for name in (
                    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS",
                )},
            }
            with (record / "output.txt").open("w") as output:
                process = subprocess.Popen(
                    ["/usr/bin/sandbox-exec", "-f", str(self.profile), "/bin/bash", "-c", command],
                    cwd=self.workspace, env=environment, stdout=output,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
            self.processes[command_id] = process
        return self.poll(command_id, wait_seconds=1)

    def poll(self, command_id, wait_seconds=1, offset=0):
        process = self.processes[command_id]
        try:
            process.wait(timeout=min(max(wait_seconds, 0), 10))
        except subprocess.TimeoutExpired:
            pass
        log = self.records / command_id / "output.txt"
        with log.open("rb") as stream:
            stream.seek(max(0, offset))
            data = stream.read(24000)
            next_offset = stream.tell()
        exit_code = process.poll()
        result = {"command_id": command_id,
                  "status": "running" if exit_code is None else "finished", "exit_code": exit_code,
                  "output": data.decode("utf-8", errors="replace"), "next_offset": next_offset}
        (self.records / command_id / "status.json").write_text(json.dumps(
            {key: value for key, value in result.items() if key != "output"}, indent=2))
        return result

    def close(self):
        for process in self.processes.values():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def create_task_server(workspace, host="127.0.0.1", port=8001):
    server = FastMCP("AMSTask", host=host, port=port)

    @server.tool()
    def task_info() -> dict:
        """Start here: independent scientific task, environment and available workflow."""
        return {
            "task": "Read README.md, TASK.md and ACCEPTANCE.md; independently implement and verify the analysis.",
            "workspace": "Relative paths refer to the task root. Write all new analysis into submission/.",
            "runtime": "python and root on PATH: Python 3.13.5, ROOT 6.34.10, NumPy 2.2.6",
            "tools": "read_file; write_file; execute (shell, returns command_id); poll_command until finished",
            "tool_names": "Use each tool's exact complete name from your client's tool catalog. Short server labels in this message are not callable aliases.",
            "reading": "If read_file returns has_more, follow next_action until the entire required document/source is read.",
            "paths": "Every execute call starts at the task root, not the previous command's cwd. Resolve input paths from the task root or the script's own location.",
            "inspection": "Inspect ROOT keys, classes and tree branches before using them; do not guess object or branch names.",
            "verification": "After exporting data, read it back and compare all bins against the source. A successful process exit does not verify values or bin mappings.",
            "isolation": "No reference solution, no credentials, no network. Supplied inputs are read-only.",
            "completion": "Run your clean-start command, inspect numerical ROOT products and editable figures, report paths and object names.",
            "budget": "One command at a time; use saved scripts, bounded output and explicit seeds. Model must perform scientific implementation itself."
        }

    @server.tool()
    def read_file(path: str, offset: int = 0, limit: int = 16000) -> dict:
        """Read a UTF-8 task or generated text file. Offset and limit are character counts."""
        text = workspace.path(path).read_text()
        end = min(len(text), max(0, offset) + min(max(limit, 1), 24000))
        result = {"text": text[max(0, offset):end], "next_offset": end,
                  "total_characters": len(text), "has_more": end < len(text)}
        if result["has_more"]:
            result["next_action"] = f"Read the rest with read_file(path={path!r}, offset={end})"
        return result

    @server.tool()
    def write_file(path: str, content: str) -> dict:
        """Create or replace an English analysis script/document inside submission/."""
        target = workspace.path(path, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return {"path": path, "characters": len(content)}

    @server.tool()
    def execute(command: str) -> dict:
        """Run a bash command in the isolated scientific workspace. Returns within seconds.

        Use python and root from PATH. A running result requires poll_command with its
        command_id. Nonzero exit code means failure; inspect output and repair your code.
        All file access stays inside the task workspace and installed scientific runtime.
        """
        return workspace.execute(command)

    @server.tool()
    def poll_command(command_id: str, wait_seconds: int = 1, offset: int = 0) -> dict:
        """Wait up to 10 seconds and read command output from a byte offset. Reuse next_offset."""
        return workspace.poll(command_id, wait_seconds, offset)

    @server.tool()
    def cancel_command(command_id: str) -> dict:
        """Stop an active command and all its descendants."""
        process = workspace.processes[command_id]
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        return workspace.poll(command_id)

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    workspace = TaskWorkspace(args.workspace, args.runtime, threads=args.threads)

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        create_task_server(workspace, port=args.port).run(transport="sse")
    finally:
        workspace.close()


if __name__ == "__main__":
    main()
