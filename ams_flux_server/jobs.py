"""Isolated analysis jobs and persistent worker lifecycle."""

import base64
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

from .storage import TERMINAL, read_json, timestamp, write_json


class JobManager:
    def __init__(self, project, runtime=None, runs=None):
        self.project = Path(project).resolve()
        self.runtime = Path(runtime or self.project / ".runtime/ams-flux").resolve()
        self.runs = Path(runs or self.project / "runs").resolve()
        self.runs.mkdir(parents=True, exist_ok=True)

    def directory(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("Invalid job ID")
        path = self.runs / job_id
        if not path.is_dir() or path.is_symlink():
            raise ValueError("Unknown job ID")
        return path

    @contextmanager
    def submission_lock(self):
        with (self.runs / ".submit.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def worker_alive(directory):
        with (directory / "lease.lock").open("a") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True

    def status(self, job_id):
        directory = self.directory(job_id)
        specification = read_json(directory / "job.json")
        state_path = directory / "state.json"
        state = read_json(state_path) if state_path.exists() else {"status": "queued", "stage": "starting"}
        if state["status"] not in TERMINAL and not self.worker_alive(directory):
            # The worker may have finished between the state read and lease check.
            if state_path.exists():
                state = read_json(state_path)
            launch = directory / "launch.json"
            alive = state["status"] in TERMINAL
            if state["status"] == "queued" and launch.exists():
                try:
                    os.kill(read_json(launch)["pid"], 0)
                    alive = True
                except ProcessLookupError:
                    pass
            if not alive:
                state.update(status="failed", stage="interrupted", exit_code=None,
                             error="Worker exited without a completion record", finished_at=timestamp())
                write_json(state_path, state)
        logs = {}
        paths = [directory / "process.log"]
        if specification["kind"] == "analysis":
            paths += sorted((Path(specification["submission"]) / "run_records").glob("*.log"))
        for path in paths:
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 2500))
                    logs[path.name] = stream.read().decode("utf-8", errors="replace")
        return {"job_id": job_id, "kind": specification["kind"],
                "analysis_job_id": specification.get("analysis_job_id"),
                "created_at": specification["created_at"], **state,
                "cancel_requested": (directory / "cancel_requested").exists(), "logs": logs}

    def _ensure_idle(self):
        for directory in self.runs.iterdir():
            if (re.fullmatch(r"[0-9a-f]{32}", directory.name)
                    and directory.is_dir() and (directory / "job.json").is_file()):
                state = self.status(directory.name)
                if state["status"] not in TERMINAL:
                    raise ValueError(f"Server busy with job {directory.name}; query or cancel it first")

    def _create(self, kind, analysis_job_id=None, replay=False):
        with self.submission_lock():
            self._ensure_idle()
            python = self.runtime / "bin/python"
            if not python.is_file() or not (self.runtime / "bin/root").is_file():
                raise ValueError(f"Scientific runtime is missing at {self.runtime}; install it or set --runtime")
            job_id = uuid.uuid4().hex
            directory = self.runs / job_id
            directory.mkdir()
            try:
                if kind == "analysis":
                    source = self.project / "demo_agent_task/submission"

                    def ignore(folder, names):
                        in_data = "Data" in Path(folder).relative_to(source).parts
                        return [name for name in names if name in {"run_records", "__pycache__"}
                                or (name.endswith(".root") and not in_data)]

                    submission = directory / "submission"
                    shutil.copytree(source, submission, ignore=ignore)
                else:
                    parent = self.status(analysis_job_id)
                    if parent["kind"] != "analysis" or parent["status"] != "succeeded":
                        raise ValueError("Validation requires a succeeded analysis job")
                    submission = Path(read_json(self.directory(analysis_job_id) / "job.json")["submission"])
                write_json(directory / "job.json", {
                    "job_id": job_id, "kind": kind, "created_at": timestamp(),
                    "submission": str(submission), "analysis_job_id": analysis_job_id, "replay": replay,
                })
                environment = os.environ.copy()
                for name in ("PYTHONHOME", "PYTHONPATH"):
                    environment.pop(name, None)
                environment.update(PATH=str(self.runtime / "bin") + os.pathsep + environment.get("PATH", ""),
                                   OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PYTHONUNBUFFERED="1")
                with (directory / "process.log").open("w") as log:
                    child = subprocess.Popen(
                        [str(python), "-B", str(Path(__file__).with_name("worker.py")), str(directory)],
                        env=environment, stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, start_new_session=True,
                    )
                write_json(directory / "launch.json", {"pid": child.pid})
                threading.Thread(target=child.wait, daemon=True).start()
            except Exception:
                if not (directory / "launch.json").exists():
                    shutil.rmtree(directory)
                raise
            return {"job_id": job_id, "kind": kind, "status": "submitted"}

    def run_analysis(self):
        return self._create("analysis")

    def validate(self, job_id, replay=False):
        return self._create("validation", analysis_job_id=job_id, replay=replay)

    def cancel(self, job_id):
        directory = self.directory(job_id)
        state = self.status(job_id)
        if state["status"] in TERMINAL:
            return state
        (directory / "cancel_requested").touch()
        deadline = time.monotonic() + 5
        signalled = False
        while time.monotonic() < deadline:
            state = self.status(job_id)
            if state["status"] in TERMINAL:
                if signalled and state["status"] == "cancelled":
                    # Descendants can outlive the direct child after ignoring SIGTERM.
                    try:
                        os.killpg(state["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                return state
            if not signalled and state.get("pid") and self.worker_alive(directory):
                try:
                    os.killpg(state["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
                signalled = True
            time.sleep(.05)
        if state.get("pid") and self.worker_alive(directory):
            try:
                os.killpg(state["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            while self.worker_alive(directory):
                time.sleep(.05)
            saved = read_json(directory / "state.json")
            saved.update(status="cancelled", exit_code=-signal.SIGKILL, finished_at=timestamp(),
                         error="Worker process group required forced termination")
            write_json(directory / "state.json", saved)
        return self.status(job_id)

    def result(self, job_id, artifact):
        directory = self.directory(job_id)
        state = self.status(job_id)
        if state["status"] != "succeeded":
            raise ValueError(f"Job is {state['status']}; results require a succeeded job")
        if artifact not in state.get("artifacts", []):
            raise ValueError(f"Unknown artifact; choose from {state.get('artifacts', [])}")
        specification = read_json(directory / "job.json")
        submission = Path(specification["submission"])
        if artifact == "spectrum":
            return read_json(directory / "artifacts/spectrum.json")
        if artifact == "report":
            return {"mime_type": "text/markdown", "text": (submission / "REPORT.md").read_text()}
        if artifact == "acceptance":
            return read_json(directory / "acceptance.json")
        if artifact == "root":
            path = submission / "01_Flux_Formula/results.root"
            return {"filename": "results.root", "mime_type": "application/x-root",
                    "encoding": "base64", "data": base64.b64encode(path.read_bytes()).decode("ascii")}
        name = "spectrum_canvas.png" if artifact == "spectrum_plot" else "spectrum_e3_canvas.png"
        return directory / "artifacts" / name
