"""Atomic job records shared by the service and detached workers."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile

TERMINAL = {"succeeded", "failed", "cancelled"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
