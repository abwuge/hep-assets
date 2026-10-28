"""Task records, input transfer and analysis result delivery."""

import base64
import fcntl
import json
import mimetypes
import os
import re
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from . import execution
from .execution import read_json, write_json

CHUNK_BYTES = 256 * 1024
AREAS = {"inputs", "work", "outputs"}


class TaskBusyError(ValueError):
    def __init__(self, task_id: str, command: dict):
        super().__init__("A command is running; poll or cancel it first")
        self.details = {
            "code": "task_busy",
            "task_id": task_id,
            "command_id": command["command_id"],
            "command_status": command["status"],
            "next_action": (
                "Call poll_command with this task_id and command_id until terminal "
                "status, then retry. Use cancel_command only to stop the command."
            ),
        }


class TaskManager:
    def __init__(
        self,
        storage: Path,
        threads: int = 2,
        assets: Path | None = None,
    ):
        self.storage = storage.resolve()
        self.storage.mkdir(parents=True, exist_ok=True)
        if threads < 1:
            raise ValueError("threads must be positive")
        self.threads = threads
        self.assets = assets.resolve(strict=True) if assets is not None else None
        if self.assets is not None:
            if not self.assets.is_dir():
                raise ValueError("Assets must be a directory")
            if self.assets.is_relative_to(self.storage) or self.storage.is_relative_to(
                self.assets
            ):
                raise ValueError("Assets and task storage must be separate directories")
            if any(
                path.is_symlink() and not path.resolve().is_relative_to(self.assets)
                for path in self.assets.rglob("*")
            ):
                raise ValueError("Asset symlinks must stay inside the assets directory")

    def task(self, task_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise ValueError("Invalid task_id")
        task = self.storage / task_id
        if task.is_symlink() or not (task / "task.json").is_file():
            raise ValueError("Unknown task_id; use list_tasks")
        return task

    @contextmanager
    def locked(self, task_id: str) -> Generator[Path]:
        task = self.task(task_id)
        with (task / "task.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield task

    @staticmethod
    def event(task: Path, action: str, **details: object) -> None:
        with (task / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"time": time.time(), "action": action, **details}) + "\n"
            )

    @staticmethod
    def working(task: Path) -> None:
        record = read_json(task / "task.json")
        record.update(status="working", updated_at=time.time())
        record.pop("completion", None)
        write_json(task / "task.json", record)

    @staticmethod
    def inside(root: Path, name: str) -> Path:
        if Path(name).is_absolute():
            raise ValueError("Use a relative path")
        path = (root / name).resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError("Path must stay inside its task directory")
        return path

    def file(self, task: Path, name: str, *, write: bool = False) -> Path:
        parts = Path(name).parts
        if parts and parts[0] == "assets":
            if write or self.assets is None:
                raise ValueError("Assets are read-only and must be configured")
            if len(parts) == 1:
                return self.assets
            return self.inside(self.assets, str(Path(*parts[1:])))
        workspace = task / "workspace"
        path = self.inside(workspace, name)
        area = path.relative_to(workspace).parts[0]
        if area not in (AREAS - {"inputs"} if write else AREAS):
            raise ValueError("Read inputs/, work/, outputs/; write work/ or outputs/")
        return path

    @staticmethod
    def commands(task: Path) -> list[dict]:
        records = [
            {
                **execution.command_status(path),
                "command": read_json(path / "command.json")["command"],
            }
            for path in (task / "commands").iterdir()
            if path.is_dir()
        ]
        return sorted(records, key=lambda item: item["started_at"])

    def idle(self, task: Path) -> None:
        for command in self.commands(task):
            if command["status"] not in execution.TERMINAL:
                raise TaskBusyError(task.name, command)

    @staticmethod
    def status(record: dict, commands: list[dict]) -> str:
        if record["status"] == "working" and commands:
            latest = commands[-1]["status"]
            if latest not in execution.TERMINAL:
                return "running"
            if latest != "succeeded":
                return "needs_attention"
        return record["status"]

    def create(self, request: str, title: str = "") -> dict:
        if not request.strip():
            raise ValueError("Provide the user's analysis request")
        task_id = uuid.uuid4().hex
        task = self.storage / task_id
        task.mkdir()
        for name in (*AREAS, ".home", ".tmp"):
            (task / "workspace" / name).mkdir(parents=True)
        (task / "commands").mkdir()
        (task / "uploads").mkdir()
        now = time.time()
        record = {
            "task_id": task_id,
            "title": title,
            "request": request,
            "status": "working",
            "created_at": now,
            "updated_at": now,
        }
        write_json(task / "task.json", record)
        self.event(task, "create")
        return {**record, "next_action": "Receive the files supplied by the user."}

    def list_tasks(self, offset: int = 0, limit: int = 20) -> dict:
        records = []
        for path in self.storage.iterdir():
            if (
                path.is_dir()
                and not path.is_symlink()
                and (path / "task.json").is_file()
            ):
                record = read_json(path / "task.json")
                record["status"] = self.status(record, self.commands(path))
                records.append(record)
        records.sort(key=lambda item: item["created_at"], reverse=True)
        start = max(0, offset)
        end = start + min(max(limit, 1), 100)
        return {
            "tasks": [
                {key: item[key] for key in ("task_id", "title", "status", "created_at")}
                for item in records[start:end]
            ],
            "next_offset": min(end, len(records)),
            "has_more": end < len(records),
        }

    def info(self, task_id: str) -> dict:
        with self.locked(task_id) as task:
            commands = self.commands(task)
            record = read_json(task / "task.json")
            inputs = self.files(task_id, "inputs")
            return {
                **record,
                "status": self.status(record, commands),
                "commands": commands[-20:],
                "command_count": len(commands),
                "inputs": inputs,
                "recent_activity": [
                    json.loads(line)
                    for line in (task / "events.jsonl").read_text().splitlines()[-20:]
                ],
                "uploads": [
                    {
                        "name": str(path.relative_to(task / "uploads"))[:-5],
                        "next_offset": path.stat().st_size,
                    }
                    for path in (task / "uploads").rglob("*.part")
                    if path.is_file()
                ],
                "assets": str(self.assets) if self.assets is not None else None,
            }

    def files(
        self, task_id: str, path: str = ".", offset: int = 0, limit: int = 100
    ) -> dict:
        task = self.task(task_id)
        workspace = task / "workspace"
        names = sorted(AREAS | ({"assets"} if self.assets is not None else set()))
        roots = (
            [self.file(task, name) for name in names]
            if path == "."
            else [self.file(task, path)]
        )
        files = []
        for root in roots:
            if not root.exists():
                raise ValueError(f"Path does not exist: {path}")
            for item in [root] if root.is_file() else root.rglob("*"):
                relative = (
                    str(Path("assets") / item.relative_to(self.assets))
                    if self.assets is not None and item.is_relative_to(self.assets)
                    else str(item.relative_to(workspace))
                )
                if item.is_file() and not item.is_symlink():
                    self.file(task, relative)
                    files.append(self.describe(item, relative))
        files.sort(key=lambda item: item["path"])
        start = max(0, offset)
        end = start + min(max(limit, 1), 500)
        return {
            "files": files[start:end],
            "next_offset": min(end, len(files)),
            "has_more": end < len(files),
        }

    @staticmethod
    def describe(path: Path, relative: str) -> dict:
        return {
            "path": relative,
            "bytes": path.stat().st_size,
            "mime_type": mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
        }

    def upload_input(
        self,
        task_id: str,
        name: str,
        data_base64: str,
        offset: int = 0,
        final: bool = True,
    ) -> dict:
        if len(data_base64) > (CHUNK_BYTES + 2) // 3 * 4:
            raise ValueError(f"Upload at most {CHUNK_BYTES} decoded bytes per call")
        data = base64.b64decode(data_base64, validate=True)
        if len(data) > CHUNK_BYTES:
            raise ValueError(f"Upload at most {CHUNK_BYTES} decoded bytes per call")
        with self.locked(task_id) as task:
            self.idle(task)
            destination = self.inside(task / "workspace/inputs", name)
            pending = self.inside(task / "uploads", name + ".part")
            if destination.exists():
                raise ValueError("Input already exists; choose a new name")
            pending.parent.mkdir(parents=True, exist_ok=True)
            size = pending.stat().st_size if pending.exists() else 0
            if offset != size:
                raise ValueError(f"Upload offset mismatch; resume at {size}")
            with pending.open("ab") as stream:
                stream.write(data)
            if final:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(pending, destination)
            self.working(task)
            self.event(task, "upload_input", name=name, bytes=len(data), final=final)
            return {
                "path": f"inputs/{name}",
                "next_offset": size + len(data),
                "complete": final,
            }

    def read_file(self, task_id: str, path: str, offset: int, limit: int) -> dict:
        text = self.file(self.task(task_id), path).read_text(encoding="utf-8")
        start = max(0, offset)
        end = min(len(text), start + min(max(limit, 1), 24000))
        return {
            "text": text[start:end],
            "next_offset": end,
            "has_more": end < len(text),
            "total_characters": len(text),
        }

    def write_file(self, task_id: str, path: str, content: str) -> dict:
        with self.locked(task_id) as task:
            self.idle(task)
            target = self.file(task, path, write=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self.working(task)
            self.event(task, "write_file", path=path)
            return self.describe(target, path)

    def execute(self, task_id: str, command: str, timeout_seconds: int = 0) -> dict:
        if not command.strip() or timeout_seconds < 0:
            raise ValueError("Provide a command and a nonnegative timeout")
        with self.locked(task_id) as task:
            self.idle(task)
            if any((task / "uploads").rglob("*.part")):
                raise ValueError("Finish pending input uploads before execution")
            self.working(task)
            result = execution.submit(
                task,
                command,
                self.threads,
                timeout_seconds,
                self.assets,
            )
            self.event(task, "execute", command_id=result["command_id"])
            return result

    def command(self, task_id: str, command_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", command_id):
            raise ValueError("Invalid command_id")
        directory = self.task(task_id) / "commands" / command_id
        if not (directory / "command.json").is_file():
            raise ValueError("Unknown command_id for this task")
        return directory

    def cancel_task(self, task_id: str) -> dict:
        with self.locked(task_id) as task:
            for command in self.commands(task):
                if command["status"] not in execution.TERMINAL:
                    execution.cancel(self.command(task_id, command["command_id"]))
            record = read_json(task / "task.json")
            record.update(
                status="cancelled", updated_at=time.time(), cancelled_at=time.time()
            )
            record.pop("completion", None)
            write_json(task / "task.json", record)
            self.event(task, "cancel_task")
        return self.info(task_id)

    def download(self, task_id: str, path: str, offset: int, limit: int) -> dict:
        target = self.file(self.task(task_id), path)
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        with target.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(min(max(limit, 1), CHUNK_BYTES))
            next_offset = stream.tell()
        return {
            **self.describe(target, path),
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
            "next_offset": next_offset,
            "has_more": next_offset < target.stat().st_size,
        }

    def finish(self, task_id: str, summary: str, artifacts: list[str]) -> dict:
        if not summary.strip() or not artifacts:
            raise ValueError(
                "Provide a result summary and at least one output artifact"
            )
        with self.locked(task_id) as task:
            self.idle(task)
            if any((task / "uploads").rglob("*.part")):
                raise ValueError(
                    "Finish pending input uploads before completing the task"
                )
            commands = self.commands(task)
            if commands and commands[-1]["status"] != "succeeded":
                raise ValueError(
                    "Resolve the last failed/cancelled command before completion"
                )
            outputs = []
            for name in artifacts:
                path = self.file(task, name)
                if not path.is_relative_to(task / "workspace/outputs"):
                    raise ValueError("Deliver result files under outputs/")
                if not path.is_file() or not path.stat().st_size:
                    raise ValueError(f"Missing or empty result: {name}")
                outputs.append(self.describe(path, name))
            record = read_json(task / "task.json")
            record.update(
                status="completed",
                updated_at=time.time(),
                completion={"summary": summary, "artifacts": outputs},
            )
            write_json(task / "task.json", record)
            self.event(task, "finish", artifacts=artifacts)
            return record
