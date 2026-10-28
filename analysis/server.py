"""Provide analysis workspaces through the official SCP Hub."""

import argparse
import binascii
import functools
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from scp.server.fastmcp import FastMCP, Image

from . import execution
from .tasks import CHUNK_BYTES, TaskManager
from .transfers import Transfers, http_base_urls


def guarded[**P, R](function: Callable[P, R]) -> Callable[P, R | dict]:
    """Keep tool failures explicit across the Hub's content-only response API."""

    @functools.wraps(function)
    def call(*args: P.args, **kwargs: P.kwargs) -> R | dict:
        try:
            result = function(*args, **kwargs)
            return {"ok": True, **result} if isinstance(result, dict) else result
        except (ValueError, OSError, KeyError, binascii.Error) as error:
            return {
                "ok": False,
                "error": str(error),
                "error_type": type(error).__name__,
            }

    return call


def create_server(
    manager: TaskManager,
    port: int = 8001,
    public_url: str | None = None,
    host: str = "127.0.0.1",
) -> FastMCP:
    server = FastMCP("HEPAnalysis", host=host, port=port)
    transfers = Transfers(manager, http_base_urls(host, port, public_url))
    transfers.attach(server)

    @server.tool()
    @guarded
    def analysis_info() -> dict:
        """Start here for the user-data-to-analysis workflow and transfer methods."""
        return {
            "workflow": [
                "Create a task from the user's request. Clarify missing assumptions.",
                "Receive the user's files using the client's file-access tools.",
                "List assets/ for shared data and references; read them in place.",
                "Inspect schemas, ROOT keys/branches, units and data quality.",
                "Write code under work/. Execute, poll, inspect errors and repair.",
                "Verify numbers and figures. Save results/report under outputs/.",
                "Complete the task; return findings, previews and result files.",
            ],
            "paths": (
                "inputs/ is read-only; work/ and outputs/ are writable. "
                "Commands start at the task's workspace root. "
                "Shared assets are read-only; use $ASSETS_DIR in commands."
            ),
            "assets": {
                "available": manager.assets is not None,
                "path": "assets/" if manager.assets is not None else None,
                "command_environment": "ASSETS_DIR",
            },
            "data_transfer": {
                "preferred": "http",
                "http_available": bool(transfers.base_urls),
                "http_base_url": transfers.base_url,
                "http_base_urls": transfers.base_urls,
                "warnings": (
                    []
                    if transfers.base_urls
                    else [
                        {
                            "code": "http_no_addresses",
                            "message": "No listener IP found; use MCP transfer.",
                            "fallback": "mcp",
                        }
                    ]
                ),
                "inputs": (
                    "Read user-supplied files with the client's file tools. "
                    "Prefer create_upload, select from urls, then HTTP PUT. "
                    "If the client cannot reach HTTP, warn and "
                    "use upload_input with next_offset; finalize the last chunk."
                ),
                "results": (
                    "Prefer result_link and a reachable URL from urls, then HTTP GET. "
                    "If HTTP is unavailable, warn and use download_file. "
                    "Confirm the local save before reporting completion."
                ),
                "automation": (
                    "HTTP is a machine-only byte transport, not a user-facing page. "
                    "The client must transfer and save files programmatically. "
                    "Keep bytes and URLs out of model context when supported. "
                    "MCP chunk loops require client automation for large files."
                ),
                "max_chunk_bytes": CHUNK_BYTES,
            },
            "execution": (
                "Commands survive MCP/server restarts. Poll using task_id and "
                "command_id; cancel_task stops work without deleting files."
            ),
            "errors": (
                "Check ok and command status/exit_code. "
                "Verify scientific correctness using the generated evidence."
            ),
            "recovery": (
                "Use list_tasks/get_task to resume, including pending uploads. "
                "Reuse verified files."
            ),
        }

    @server.tool()
    @guarded
    def create_task(request: str, title: str = "") -> dict:
        """Create an analysis from the user's request; then provide input files."""
        return manager.create(request, title)

    @server.tool()
    @guarded
    def list_tasks(offset: int = 0, limit: int = 20) -> dict:
        """Find stored tasks to continue work or retrieve earlier results."""
        return manager.list_tasks(offset, limit)

    @server.tool()
    @guarded
    def get_task(task_id: str) -> dict:
        """Read the request, input inventory, recent commands and delivered results."""
        return manager.info(task_id)

    @server.tool()
    @guarded
    def upload_input(
        task_id: str,
        input_name: str,
        data_base64: str,
        offset: int = 0,
        final: bool = True,
    ) -> dict:
        """Upload client-readable bytes, at most 256 KiB decoded per call.

        Reuse next_offset; set final only on the last chunk. Pending uploads are
        excluded from inputs until finalized. Never claim an attachment was
        received before the transfer succeeds.
        """
        return manager.upload_input(task_id, input_name, data_base64, offset, final)

    @server.tool()
    @guarded
    def create_upload(task_id: str, input_name: str) -> dict:
        """Prefer this for file upload: create a 24-hour, one-use HTTP PUT target.

        The client sends raw bytes programmatically. If HTTP is unavailable, warn
        and fall back to upload_input. This endpoint has no browser interface.
        """
        return transfers.create(task_id, input_name, "upload")

    @server.tool()
    @guarded
    def list_files(
        task_id: str, path: str = ".", offset: int = 0, limit: int = 100
    ) -> dict:
        """List files with sizes and MIME types; follow pagination."""
        return manager.files(task_id, path, offset, limit)

    @server.tool()
    @guarded
    def read_file(task_id: str, path: str, offset: int = 0, limit: int = 16000) -> dict:
        """Read UTF-8 inputs, assets, code or reports; offsets count characters."""
        return manager.read_file(task_id, path, offset, limit)

    @server.tool()
    @guarded
    def write_file(task_id: str, path: str, content: str) -> dict:
        """Write UTF-8 analysis code under work/ or deliverables under outputs/."""
        return manager.write_file(task_id, path, content)

    @server.tool()
    @guarded
    def execute(task_id: str, command: str, timeout_seconds: int = 0) -> dict:
        """Start analysis and return command_id immediately; poll until terminal status.

        One active command per task. Uses bash -c, with work/ and outputs/ writable,
        inputs/ and shared server files are read-only. No network or inherited
        credentials. Commands use the system PATH.
        Set shell error handling explicitly. Timeout 0 means no time limit.
        """
        return manager.execute(task_id, command, timeout_seconds)

    @server.tool()
    @guarded
    def poll_command(
        task_id: str, command_id: str, wait_seconds: int = 1, offset: int = 0
    ) -> dict:
        """Wait up to 10 seconds; follow next_offset for subsequent output bytes."""
        return execution.poll(
            manager.command(task_id, command_id), wait_seconds, offset
        )

    @server.tool()
    @guarded
    def cancel_command(task_id: str, command_id: str) -> dict:
        """Cancel one command and its descendants while keeping files and logs."""
        with manager.locked(task_id):
            return execution.cancel(manager.command(task_id, command_id))

    @server.tool()
    @guarded
    def cancel_task(task_id: str) -> dict:
        """Stop this task's active command and retain data for later continuation."""
        return manager.cancel_task(task_id)

    @server.tool()
    @guarded
    def download_file(
        task_id: str, path: str, offset: int = 0, limit: int = 65536
    ) -> dict:
        """Return a binary file in base64 chunks; reconstruct bytes on the client."""
        return manager.download(task_id, path, offset, limit)

    @server.tool()
    @guarded
    def result_link(task_id: str, path: str) -> dict:
        """Prefer this for downloads: create a 24-hour HTTP GET target under outputs/.

        The client fetches and saves bytes programmatically. If HTTP is unavailable,
        warn and fall back to download_file. Do not ask the user to open the URL.
        """
        return transfers.create(task_id, path, "download")

    @server.tool()
    @guarded
    def preview_image(task_id: str, path: str) -> Image:
        """Return a PNG/JPEG/GIF/WebP as native MCP image content (up to 5 MiB)."""
        target = manager.file(manager.task(task_id), path)
        if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            raise ValueError("Export a PNG/JPEG/GIF/WebP preview first")
        if target.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("Image exceeds 5 MiB; export a smaller preview")
        return Image(path=target)

    @server.tool()
    @guarded
    def complete_task(task_id: str, summary: str, artifacts: list[str]) -> dict:
        """Deliver existing nonempty outputs after analysis and verification.

        Summarize actual findings, assumptions and checks. This records the agent's
        completion; scientific claims must be supported by the produced evidence.
        """
        return manager.finish(task_id, summary, artifacts)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage", type=Path, default=Path(__file__).parent / ".local"
    )
    parser.add_argument(
        "--assets", type=Path, default=Path(__file__).resolve().parents[1] / "assets"
    )
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--hub", default="http://127.0.0.1:46380")
    parser.add_argument(
        "--public-url", help="Client-reachable HTTP(S) file-transfer base URL"
    )
    args = parser.parse_args()
    try:
        manager = TaskManager(args.storage, args.threads, args.assets)
        execution.check_sandbox(manager.assets)
        server = create_server(manager, args.port, args.public_url, args.host)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    stop = threading.Event()

    def register() -> None:
        try:
            deadline = time.monotonic() + 30
            while not stop.is_set():
                try:
                    with socket.create_connection(
                        (
                            ("127.0.0.1" if args.host == "0.0.0.0" else "::1")
                            if args.host in {"0.0.0.0", "::"}
                            else args.host,
                            args.port,
                        ),
                        timeout=0.2,
                    ):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Analysis server did not start") from None
                    stop.wait(0.1)
            if stop.is_set():
                return
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.post(
                    args.hub.rstrip("/") + "/register_server",
                    json={
                        "server_name": "HEPAnalysis",
                        "server_description": "Analysis workspace and result delivery",
                        "server_url": (
                            (
                                "http://127.0.0.1"
                                if args.host == "0.0.0.0"
                                else "http://[::1]"
                            )
                            if args.host in {"0.0.0.0", "::"}
                            else "http://"
                            + (f"[{args.host}]" if ":" in args.host else args.host)
                        ),
                        "server_port": args.port,
                        "type": "sse",
                    },
                )
                response.raise_for_status()
            print("HEPAnalysis registered with SCP Hub", file=sys.stderr)
        except (httpx.HTTPError, RuntimeError) as error:
            print(f"Hub registration failed: {error}", file=sys.stderr)
            if not stop.is_set():
                os.kill(os.getpid(), signal.SIGTERM)

    registration = threading.Thread(target=register, daemon=True)
    registration.start()
    try:
        server.run(transport="sse")
    finally:
        stop.set()
        registration.join(timeout=1)


if __name__ == "__main__":
    main()
