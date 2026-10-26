"""Start the official SCP Hub with project-local state."""

import os
from pathlib import Path


def main():
    state = Path(__file__).resolve().parent / ".local"
    state.mkdir(parents=True, exist_ok=True)
    os.environ["STORAGE_FILE_PATH"] = str(state / "servers.json")
    os.environ.setdefault("REDIS_HOST", "127.0.0.1")
    from scp.hub.hub_server import run

    run(host="127.0.0.1", port=46380)


if __name__ == "__main__":
    main()
