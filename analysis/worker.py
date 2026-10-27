"""Run a command supervisor independently of the MCP server process."""

import os
import sys
from pathlib import Path

from .execution import run_worker

if __name__ == "__main__":
    with os.fdopen(int(sys.argv[2])):
        run_worker(Path(sys.argv[1]))
