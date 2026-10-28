"""Start the official SCP Hub and its server-side MCP endpoint together."""

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import uvicorn


async def serve(host: str, port: int, hub_port: int, storage: Path) -> None:
    if __package__:
        from .mcp_bridge import create_app
    else:
        from mcp_bridge import create_app

    storage.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "STORAGE_FILE_PATH": str(storage / "servers.json")}
    env.setdefault("REDIS_HOST", "127.0.0.1")
    server: uvicorn.Server | None = None
    stopping = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True
        if server is not None:
            server.should_exit = True

    previous = {
        signum: signal.signal(signum, stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    hub = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from scp.hub.hub_server import run; "
            f"run(host='127.0.0.1', port={hub_port})",
        ],
        env=env,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            if stopping:
                return
            if hub.poll() is not None:
                raise RuntimeError(f"Official Hub exited with code {hub.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", hub_port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Official Hub did not start within 20 seconds"
                    ) from None
                await asyncio.sleep(0.1)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(f"http://127.0.0.1:{hub_port}"),
                host=host,
                port=port,
                log_level="info",
            )
        )

        async def monitor_hub():
            while hub.poll() is None:
                await asyncio.sleep(0.2)
            print(f"Official Hub exited with code {hub.returncode}", file=sys.stderr)
            server.should_exit = True

        monitor = asyncio.create_task(monitor_hub())
        try:
            await server.serve()
            if hub.poll() is not None:
                raise RuntimeError(
                    "Official Hub stopped; MCP endpoint has been stopped"
                )
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
    finally:
        if hub.poll() is None:
            hub.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(hub.wait), timeout=5)
            except TimeoutError:
                hub.kill()
                await asyncio.to_thread(hub.wait)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="MCP bind address")
    parser.add_argument("--port", type=int, default=46381, help="MCP port")
    parser.add_argument(
        "--hub-port", type=int, default=46380, help="Internal official Hub port"
    )
    parser.add_argument(
        "--storage", type=Path, default=Path(__file__).resolve().parent / ".local"
    )
    args = parser.parse_args()
    if args.port == args.hub_port:
        parser.error("MCP and internal Hub ports must differ")
    try:
        asyncio.run(serve(args.host, args.port, args.hub_port, args.storage))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
