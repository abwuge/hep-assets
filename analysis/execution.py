"""Persistent command execution in isolated scientific workspaces."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

TERMINAL = {"succeeded", "failed", "cancelled"}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def command_status(directory: Path) -> dict:
    state = read_json(directory / "status.json")
    if state["status"] not in TERMINAL:
        with (directory / "lease.lock").open("a") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return state
            state = read_json(directory / "status.json")
            if state["status"] not in TERMINAL:
                state.update(
                    status="failed",
                    error="Command supervisor exited without a completion record",
                    finished_at=time.time(),
                )
                write_json(directory / "status.json", state)
    return state


def submit(
    task: Path,
    command: str,
    threads: int,
    timeout_seconds: int,
    assets: Path | None = None,
) -> dict:
    command_id = uuid.uuid4().hex
    directory = task / "commands" / command_id
    directory.mkdir(parents=True)
    write_json(
        directory / "command.json",
        {
            "command": command,
            "workspace": str(task / "workspace"),
            "threads": threads,
            "timeout_seconds": timeout_seconds,
            "assets": str(assets) if assets is not None else None,
        },
    )
    state = {
        "command_id": command_id,
        "status": "queued",
        "exit_code": None,
        "started_at": time.time(),
    }
    write_json(directory / "status.json", state)
    # The inherited lease covers the interval before the worker starts Python.
    with (directory / "lease.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        with (directory / "supervisor.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "analysis.worker",
                    str(directory),
                    str(lease.fileno()),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(lease.fileno(),),
            )
        # Reap the supervisor without tying its lifetime to the tool connection.
        import threading

        threading.Thread(target=process.wait, daemon=True).start()
    return state


def poll(directory: Path, wait_seconds: int = 1, offset: int = 0) -> dict:
    deadline = time.monotonic() + min(max(wait_seconds, 0), 10)
    while True:
        state = command_status(directory)
        if state["status"] in TERMINAL or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    output = directory / "output.txt"
    data, next_offset, size = b"", max(0, offset), 0
    if output.exists():
        with output.open("rb") as stream:
            stream.seek(next_offset)
            data = stream.read(24000)
            next_offset = stream.tell()
            size = os.fstat(stream.fileno()).st_size
    return {
        **state,
        "output": data.decode("utf-8", errors="replace"),
        "next_offset": next_offset,
        "has_more": next_offset < size,
    }


def cancel(directory: Path) -> dict:
    if command_status(directory)["status"] not in TERMINAL:
        (directory / "cancel").touch()
    return poll(directory, 10)


def sandbox_command(
    directory: Path,
    workspace: Path,
    command: str,
    assets: Path | None = None,
) -> list[str]:
    """Apply the same filesystem and network policy with native OS isolation."""
    writable = [workspace / name for name in ("work", "outputs", ".home", ".tmp")]
    match sys.platform:
        case "linux":
            executable = shutil.which("bwrap", path=os.defpath)
            if executable is None:
                raise ValueError("Install bubblewrap (bwrap) to run analysis on Linux")
            arguments = [
                executable,
                "--unshare-all",
                "--die-with-parent",
                "--new-session",
                "--cap-drop",
                "ALL",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
            ]
            for name in (
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
                "/etc/ld.so.cache",
                "/etc/alternatives",
                "/etc/fonts",
                "/etc/localtime",
            ):
                if Path(name).exists():
                    arguments.extend(["--ro-bind", name, name])
            for path in (workspace, assets):
                if path is not None:
                    arguments.extend(["--ro-bind", str(path), str(path)])
            for path in writable:
                arguments.extend(["--bind", str(path), str(path)])
            arguments.extend(
                [
                    "--chdir",
                    str(workspace),
                    "--remount-ro",
                    "/",
                    "--",
                    "/bin/bash",
                    "-c",
                    command,
                ]
            )
            return arguments
        case "darwin":
            if not Path("/usr/bin/sandbox-exec").is_file():
                raise ValueError("macOS sandbox-exec is unavailable")
            quote = json.dumps
            readable = [
                workspace,
                Path("/System"),
                Path("/usr"),
                Path("/bin"),
                Path("/sbin"),
                Path("/dev"),
                Path("/Library/Developer"),
                Path("/Library/Apple"),
                Path("/private/etc"),
                Path("/private/var/db"),
                Path("/private/var/run"),
            ]
            if assets is not None:
                readable.append(assets)
            profile = directory / "sandbox.sb"
            profile.write_text(
                "(version 1)\n(allow default)\n(deny network*)\n(deny signal)\n"
                "(deny process-info* (target others))\n"
                '(deny file-read-data (require-all (require-not (literal "/")) '
                + " ".join(
                    f"(require-not (subpath {quote(str(path.resolve()))}))"
                    for path in readable
                )
                + "))\n(deny file-write* (require-all "
                + " ".join(
                    f"(require-not (subpath {quote(str(path))}))" for path in writable
                )
                + '(require-not (literal "/dev/null"))))\n',
                encoding="utf-8",
            )
            return [
                "/usr/bin/sandbox-exec",
                "-f",
                str(profile),
                "/bin/bash",
                "-c",
                command,
            ]
        case _:
            raise ValueError("Analysis isolation supports Linux and macOS")


def check_sandbox(assets: Path | None = None) -> None:
    """Fail startup if the host cannot create the required sandbox."""
    with tempfile.TemporaryDirectory(prefix="hep-sandbox-") as name:
        directory = Path(name).resolve()
        workspace = directory / "workspace"
        for area in ("inputs", "work", "outputs", ".home", ".tmp"):
            (workspace / area).mkdir(parents=True)
        result = subprocess.run(
            sandbox_command(directory, workspace, "true", assets),
            cwd=workspace,
            env={"PATH": os.defpath},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            raise ValueError(f"Cannot start analysis sandbox: {result.stderr.strip()}")


def run_worker(directory: Path) -> None:
    specification = read_json(directory / "command.json")
    workspace = Path(specification["workspace"])
    assets = Path(specification["assets"]) if specification.get("assets") else None
    environment = {
        "PATH": os.defpath,
        "HOME": str(workspace / ".home"),
        "TMPDIR": str(workspace / ".tmp"),
        "LANG": "C.UTF-8" if sys.platform == "linux" else "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        **dict.fromkeys(
            (
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            ),
            str(specification["threads"]),
        ),
    }
    if assets is not None:
        environment["ASSETS_DIR"] = str(assets)
    state = read_json(directory / "status.json")
    interrupted = False

    def terminate(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    process = None
    try:
        with (directory / "output.txt").open("wb") as output:
            process = subprocess.Popen(
                sandbox_command(directory, workspace, specification["command"], assets),
                cwd=workspace,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            state.update(status="running")
            write_json(directory / "status.json", state)
            timeout = specification["timeout_seconds"]
            deadline = time.monotonic() + timeout if timeout else float("inf")
            while process.poll() is None:
                if interrupted or (directory / "cancel").exists():
                    state["status"] = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    state.update(status="failed", error="Command time limit exceeded")
                    break
                time.sleep(0.05)
            if state["status"] == "running":
                state["status"] = "succeeded" if process.returncode == 0 else "failed"
    except Exception as error:
        state.update(status="failed", error=str(error))
    finally:
        if process is not None:
            # Also terminate descendants when the shell has already exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            state["exit_code"] = process.wait()
        state["finished_at"] = time.time()
        write_json(directory / "status.json", state)
