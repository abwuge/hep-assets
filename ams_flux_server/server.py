"""Expose the existing scientific runner through the SCP SDK."""

import argparse
import asyncio
from pathlib import Path
from typing import Any, Literal

from scp.server.fastmcp import FastMCP, Image

from .jobs import JobManager


def create_server(manager, host="127.0.0.1", port=8000):
    server = FastMCP("AMSFlux", host=host, port=port)

    @server.tool()
    def describe_analysis() -> dict:
        """Describe the fixed electron-plus-positron flux reconstruction and artifacts."""
        return {
            "task_version": "0.6", "analysis": "AMS electron-plus-positron differential flux",
            "inputs": "Six supplied processed ROOT files; fixed demo methods and configuration",
            "energy_range_GeV": [98.08, 2000], "energy_bins": 14,
            "runtime": "Python 3.13.5, ROOT 6.34.10, NumPy 2.2.6",
            "execution": "One active job; detached worker and isolated analysis workspace",
            "workflow": "run_analysis -> get_status -> get_result; validate_job runs a separate check",
            "artifacts": ["spectrum", "report", "root", "spectrum_plot", "spectrum_e3_plot"],
            "trigger": "Absolute central correction applied once; its uncertainty excluded by task",
        }

    @server.tool()
    async def run_analysis() -> dict:
        """Submit a clean demo analysis; return a job ID without waiting for computation."""
        return await asyncio.to_thread(manager.run_analysis)

    @server.tool()
    async def get_status(job_id: str) -> dict:
        """Read persistent job status, stage, exit code, artifacts and bounded log tails."""
        return await asyncio.to_thread(manager.status, job_id)

    @server.tool()
    async def get_result(job_id: str, artifact: Literal[
        "spectrum", "report", "root", "spectrum_plot", "spectrum_e3_plot", "acceptance"
    ] = "spectrum") -> Any:
        """Read a succeeded job's result: JSON, report, PNG image or base64 ROOT file.

        spectrum includes bin edges, representative energies, validity, flux, standard
        deviations, both covariance matrices, units and uncertainty scope. acceptance
        belongs to a validation job. ROOT data can be decoded to recover results.root.
        """
        result = await asyncio.to_thread(manager.result, job_id, artifact)
        return Image(path=str(result)) if isinstance(result, Path) else result

    @server.tool()
    async def validate_job(job_id: str, replay: bool = False) -> dict:
        """Submit acceptance checking for a succeeded analysis; optionally run two clean replays."""
        return await asyncio.to_thread(manager.validate, job_id, replay)

    @server.tool()
    async def cancel_job(job_id: str) -> dict:
        """Cancel the job and its process group; completed jobs are returned unchanged."""
        return await asyncio.to_thread(manager.cancel, job_id)

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runtime", type=Path, help="Scientific environment prefix")
    parser.add_argument("--runs", type=Path, help="Persistent job directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    manager = JobManager(arguments.project, arguments.runtime, arguments.runs)
    create_server(manager, arguments.host, arguments.port).run(transport="sse")


if __name__ == "__main__":
    main()
