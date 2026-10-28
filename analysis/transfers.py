"""Token-scoped HTTP file transfer alongside the analysis MCP endpoint."""

import ipaddress
import os
import re
import secrets
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil
from scp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from .execution import read_json, write_json
from .tasks import TaskManager


def http_base_urls(host: str, port: int, public_url: str | None = None) -> list[str]:
    """Advertise bound IPs; explicit reverse-proxy addresses take precedence."""
    if public_url:
        return [public_url.rstrip("/")]
    if host in {"0.0.0.0", "::"}:
        family = socket.AF_INET if host == "0.0.0.0" else socket.AF_INET6
        stats = psutil.net_if_stats()
        addresses = {
            address.address
            for interface, entries in psutil.net_if_addrs().items()
            if interface in stats and stats[interface].isup
            for address in entries
            if address.family == family
            and not ipaddress.ip_address(address.address).is_unspecified
            and not ipaddress.ip_address(address.address).is_multicast
        }
        hosts = sorted(
            addresses,
            key=lambda value: (
                ipaddress.ip_address(value).is_loopback,
                int(ipaddress.ip_address(value)),
                value,
            ),
        )
    else:
        hosts = [host]
    return [
        f"http://[{value.replace('%', '%25')}]:{port}"
        if ":" in value
        else f"http://{value}:{port}"
        for value in hosts
    ]


class Transfers:
    def __init__(self, manager: TaskManager, base_urls: list[str]):
        for base_url in base_urls:
            self.validate_base_url(base_url)
        self.manager = manager
        self.base_urls = [url.rstrip("/") for url in base_urls]
        self.base_url = self.base_urls[0] if self.base_urls else None
        self.grants = manager.storage / ".transfers"
        self.grants.mkdir(exist_ok=True)

    @staticmethod
    def validate_base_url(base_url: str) -> None:
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "public-url must be an HTTP(S) base URL "
                "without credentials, query or fragment"
            )

    def output(self, task: Path, name: str) -> Path:
        target = self.manager.file(task, name)
        if (
            not target.is_relative_to(task / "workspace/outputs")
            or not target.is_file()
        ):
            raise ValueError("Choose an existing file under outputs/")
        return target

    def create(self, task_id: str, name: str, direction: str) -> dict:
        if not self.base_urls:
            return {
                "ok": False,
                "error": "http_no_addresses",
                "warning": "No listener IP found; use MCP transfer.",
                "fallback_tool": "upload_input"
                if direction == "upload"
                else "download_file",
            }
        with self.manager.locked(task_id) as task:
            if direction == "upload":
                self.manager.idle(task)
                destination = self.manager.inside(task / "workspace/inputs", name)
                pending = self.manager.inside(task / "uploads", name + ".part")
                if destination.exists() or pending.exists():
                    raise ValueError(
                        "Input exists or has a pending MCP upload; choose a new name"
                    )
            elif direction == "download":
                self.output(task, name)
            else:
                raise ValueError("Unknown transfer direction")
            token = secrets.token_urlsafe(32)
            now = time.time()
            expires = now + 86400
            write_json(
                self.grants / (token + ".json"),
                {
                    "task_id": task_id,
                    "name": name,
                    "direction": direction,
                    "expires_at": expires,
                    "created_at": now,
                    "used": False,
                },
            )
        urls = [f"{base}/files/{token}" for base in self.base_urls]
        return {
            "url": urls[0],
            "urls": urls,
            "expires_at": expires,
            "direction": direction,
            "name": name,
            "method": "PUT" if direction == "upload" else "GET",
            "next_action": (
                (
                    "Use a client-local HTTP client to PUT the original file bytes "
                    "to a reachable target from urls; confirm upload success. "
                    if direction == "upload"
                    else "Use a client-local HTTP client to GET bytes from a "
                    "reachable target in urls and save the file locally. "
                )
                + "Do not use server execute for client file transfers. "
                "If none of the HTTP targets works, warn and use "
                + (
                    "upload_input with next_offset."
                    if direction == "upload"
                    else "download_file with next_offset."
                )
            ),
        }

    def grant(self, token: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ValueError("Invalid file link")
        record = read_json(self.grants / (token + ".json"))
        if record["expires_at"] < time.time() or record["used"]:
            raise ValueError("File link expired or already used; request a new link")
        task = self.manager.task(record["task_id"])
        if (
            record["direction"] == "upload"
            and read_json(task / "task.json").get("cancelled_at", 0)
            > record["created_at"]
        ):
            raise ValueError("Upload cancelled; create a new upload link to continue")
        return record

    def attach(self, server: FastMCP) -> None:
        @server.custom_route("/files/{token}", methods=["GET", "PUT"])
        async def transfer(request: Request) -> Response:
            token = request.path_params["token"]
            try:
                record = self.grant(token)
                expected = "PUT" if record["direction"] == "upload" else "GET"
                if request.method != expected:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "method_not_allowed",
                            "method": expected,
                        },
                        405,
                        headers={"Allow": expected, "Cache-Control": "no-store"},
                    )
                if expected == "GET":
                    target = self.output(
                        self.manager.task(record["task_id"]), record["name"]
                    )
                    return FileResponse(
                        target,
                        filename=target.name,
                        headers={"Cache-Control": "no-store"},
                    )
                return await self.receive(request, token, record)
            except FileNotFoundError:
                return JSONResponse({"ok": False, "error": "file_not_found"}, 404)
            except (OSError, ValueError) as error:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": str(error),
                        "error_type": type(error).__name__,
                    },
                    400,
                    headers={"Cache-Control": "no-store"},
                )

    async def receive(self, request: Request, token: str, record: dict) -> Response:
        task_id, name = record["task_id"], record["name"]
        with self.manager.locked(task_id) as task:
            self.manager.idle(task)
            descriptor, temporary = tempfile.mkstemp(
                prefix="http-", suffix=".part", dir=task / "uploads"
            )
        staging = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in request.stream():
                    stream.write(chunk)
            with self.manager.locked(task_id):
                self.manager.idle(task)
                self.grant(token)
                destination = self.manager.inside(task / "workspace/inputs", name)
                pending = self.manager.inside(task / "uploads", name + ".part")
                if destination.exists() or pending.exists():
                    raise ValueError(
                        "Input exists or has a pending MCP upload; choose a new name"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, destination)
                self.manager.working(task)
                self.manager.event(task, "http_upload", name=name)
                record["used"] = True
                write_json(self.grants / (token + ".json"), record)
            result = {
                "ok": True,
                "task_id": task_id,
                "path": f"inputs/{name}",
                "bytes": destination.stat().st_size,
            }
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        finally:
            staging.unlink(missing_ok=True)
