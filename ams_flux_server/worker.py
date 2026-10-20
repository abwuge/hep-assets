"""Detached job supervisor; survives an SCP client or server disconnect."""

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys

from storage import read_json, timestamp, write_json


def run(directory):
    specification = read_json(directory / "job.json")
    state = {"status": "running", "stage": "starting", "pid": os.getpid(),
             "started_at": timestamp(), "exit_code": None}
    cancelled = False

    def request_cancel(signum, frame):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)

    def save():
        write_json(directory / "state.json", state)

    def command(stage, arguments):
        if cancelled or (directory / "cancel_requested").exists():
            raise InterruptedError("Cancellation requested")
        state["stage"] = stage
        save()
        print(f"Starting {stage}", flush=True)
        child = subprocess.Popen(arguments, cwd=specification["submission"])
        # Cover a cancellation arriving between the precheck and process launch.
        if cancelled or (directory / "cancel_requested").exists():
            child.terminate()
        code = child.wait()
        if cancelled or (directory / "cancel_requested").exists():
            state["exit_code"] = code
            raise InterruptedError("Cancellation requested")
        if code:
            raise subprocess.CalledProcessError(code, arguments)

    with (directory / "lease.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        save()
        try:
            python = sys.executable
            submission = Path(specification["submission"])
            if specification["kind"] == "analysis":
                command("analysis", [python, "-B", str(submission / "run.py")])
                command("export", [python, "-B", str(Path(__file__).with_name("export.py")),
                                   str(submission), str(directory / "artifacts")])
                state["artifacts"] = ["spectrum", "report", "root", "spectrum_plot", "spectrum_e3_plot"]
            else:
                arguments = [python, "-B", str(submission / "acceptance_check.py")]
                if specification["replay"]:
                    arguments.append("--replay")
                command("validation", arguments)
                acceptance = read_json(submission / "run_records/acceptance.json")
                if acceptance.get("status") != "PASS":
                    raise RuntimeError("Acceptance check did not pass")
                write_json(directory / "acceptance.json", acceptance)
                state["artifacts"] = ["acceptance"]
            state.update(status="succeeded", stage="completed", exit_code=0)
        except InterruptedError as error:
            state.update(status="cancelled", error=str(error))
        except Exception as error:
            state.update(status="failed", error=str(error),
                         exit_code=getattr(error, "returncode", 1))
            print(f"Job failed: {error}", flush=True)
        finally:
            state["finished_at"] = timestamp()
            save()


if __name__ == "__main__":
    run(Path(sys.argv[1]))
