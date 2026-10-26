"""Timed, isolated services for a fresh native-agent benchmark."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import httpx

from .task_server import TaskWorkspace
from .trial import free_port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    run = args.run.resolve()
    run.mkdir(parents=True, exist_ok=False)
    task = run / "task"
    shutil.copytree(project / "Agent_Task", task)
    (run / "client").mkdir()
    (run / "claude.jsonl").touch()
    processes, handles = [], []
    record = {"model": "Codex clean subagent", "client": "native Codex subagent",
              "trace_scope": "Hub tool calls and supplied task; native progress is in the parent chat",
              "route": "Codex -> temporary Hub transport -> official SCP Hub -> AMSTask SSE Server",
              "access_policy": "Native subagent authorized only to use Hub transport; scientific processes use the task filesystem sandbox",
              "task": str(task), "minutes": args.minutes, "threads": 2}

    def save():
        temporary = run / "trial.json.tmp"
        temporary.write_text(json.dumps(record, indent=2))
        temporary.replace(run / "trial.json")

    def start(command, log, env=None):
        output = (run / log).open("w")
        handles.append(output)
        process = subprocess.Popen(command, cwd=project, env=env, stdout=output, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    try:
        workspace = TaskWorkspace(task, project / ".runtime/ams-flux", threads=2)
        state = workspace.execute("python -c 'import ROOT; print(ROOT.gROOT.GetVersion())'")
        while state["status"] == "running":
            state = workspace.poll(state["command_id"], 10)
        if state["exit_code"] != 0:
            raise RuntimeError(state)
        server_port, hub_port = free_port(), free_port()
        record.update(server_port=server_port, hub=f"http://127.0.0.1:{hub_port}")
        start([sys.executable, "-m", "ams_flux_server.task_server", "--workspace", str(task),
               "--runtime", str(project / ".runtime/ams-flux"), "--threads", "2", "--port", str(server_port)], "server.log")
        env = os.environ.copy()
        env["STORAGE_FILE_PATH"] = str(run / "hub-servers.json")
        start([sys.executable, "-c", "from scp.hub.hub_server import run; "
               f"run(host='127.0.0.1',port={hub_port})"], "hub.log", env)
        with httpx.Client(timeout=5, trust_env=False) as client:
            for url in (f"http://127.0.0.1:{server_port}/sse", record["hub"] + "/tools"):
                deadline = time.monotonic() + 30
                while True:
                    try:
                        with client.stream("GET", url) as response:
                            response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(.2)
            response = client.post(record["hub"] + "/register_server", json={
                "server_name": "AMSTask", "server_description": "Independent scientific task",
                "server_url": "http://127.0.0.1", "server_port": server_port, "type": "sse"})
            response.raise_for_status()
            response = client.get(record["hub"] + "/tools?server_name=AMSTask")
            response.raise_for_status()
            (run / "discovered-tools.json").write_text(json.dumps(response.json(), indent=2))
        record.update(started_at=time.time(), deadline_at=time.time() + args.minutes * 60)
        save()
        print(json.dumps(record), flush=True)
        while time.time() < record["deadline_at"] and not (run / ".stop").exists():
            if any(process.poll() is not None for process in processes):
                record["stopped_reason"] = "service_exited"
                break
            time.sleep(.5)
        record.setdefault("stopped_reason", "completed" if (run / ".stop").exists() else "wall_time_limit")
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for output in handles:
            output.close()
        record["ended_at"] = time.time()
        save()


if __name__ == "__main__":
    main()
