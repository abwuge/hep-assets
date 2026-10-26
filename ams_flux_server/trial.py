"""Run a selected model in Claude through an SCP Hub into an isolated task server."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import httpx



def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="New trial directory")
    parser.add_argument("--model", default="Agents-A1", help="Exact model name sent to the configured API")
    parser.add_argument("--budget", type=float, default=0, help="Claude-reported USD cap; 0 disables the estimate-based cap")
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--threads", type=int, default=2, help="Numerical-library threads per scientific process")
    parser.add_argument("--resume-task", type=Path, help="Copy an earlier independent task workspace, including its partial implementation")
    parser.add_argument("--feedback-file", type=Path, help="Verified prior-run findings to include in the task prompt")
    return parser


def task_prompt(args):
    opening = (
        "Continue the existing independent scientific submission toward all requirements in TASK.md and ACCEPTANCE.md. "
        "Inspect the actual files; reuse valid results and consult the specifications relevant to your next decisions. "
        "You choose the remaining work order and implementation. No reference solution is available.\n"
        if args.resume_task else
        "Read README.md and TASK.md, complete and verify the analysis as instructed, "
        "and report the final result file paths and ROOT canvas names.\n"
        "Read ACCEPTANCE.md, required method specifications and public sources fully, including all paginated remainders.\n"
    )
    prompt = (
        opening +
        "Start with task_info. All file and execution tools are provided through SCP Hub. "
        "Independently implement the science; no reference solution is available. "
        "Write new files under submission/. Do not stop at a plan.\n"
        f"Resource limit: {args.minutes} minutes wall time, one scientific process at a time, "
        "no network inside the scientific workspace. "
        f"Use only configured model {args.model} and the supplied tools. "
        "If unable to finish, report exact remaining requirements, failed checks and actual errors."
    )
    if args.budget > 0:
        prompt += f" Client-estimated spending cap: USD {args.budget}."
    if args.resume_task:
        prompt += " This workspace contains your prior partial implementation. Read its actual files, preserve valid results, repair failed exports before using them."
    if args.feedback_file:
        prompt += "\n\nPrior-run verified findings:\n" + args.feedback_file.read_text()
    return prompt


def main():
    args = argument_parser().parse_args()
    project = Path(__file__).resolve().parents[1]
    run = args.run.resolve()
    run.mkdir(parents=True, exist_ok=False)
    task = run / "task"
    shutil.copytree(args.resume_task or project / "Agent_Task", task)
    server_port, hub_port = free_port(), free_port()
    hub_url = f"http://127.0.0.1:{hub_port}"
    processes, handles = [], []

    def start(command, log, env=None, cwd=project):
        output = (run / log).open("w")
        handles.append(output)
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    prompt = task_prompt(args)
    (run / "prompt.txt").write_text(prompt)
    config = {"mcpServers": {"scp": {"command": sys.executable, "args": [
        "-m", "ams_flux_server.hub_client", "--hub", hub_url, "--server", "AMSTask",
    ]}}}
    (run / "mcp.json").write_text(json.dumps(config, indent=2))
    record = {"model": args.model, "route": "Claude -> MCP stdio adapter -> official SCP Hub -> AMSTask SSE Server",
              "session_persistence": False,
              "hub": hub_url, "server_port": server_port, "task": str(task),
              "resumed_from": str(args.resume_task.resolve()) if args.resume_task else None,
              "budget_usd": args.budget, "minutes": args.minutes, "threads": args.threads, "started_at": time.time()}
    (run / "trial.json").write_text(json.dumps(record, indent=2))
    try:
        # Fail before starting the model if execution isolation is unavailable.
        from .task_server import TaskWorkspace
        workspace = TaskWorkspace(task, project / ".runtime/ams-flux", threads=args.threads)
        preflight = workspace.execute("python -c 'import ROOT; print(ROOT.gROOT.GetVersion())'")
        while preflight["status"] == "running":
            preflight = workspace.poll(preflight["command_id"], 10)
        if preflight["exit_code"] != 0:
            raise RuntimeError(f"Sandbox preflight failed: {preflight}")
        start([sys.executable, "-m", "ams_flux_server.task_server", "--workspace", str(task),
               "--runtime", str(project / ".runtime/ams-flux"), "--port", str(server_port),
               "--threads", str(args.threads)], "server.log")
        environment = os.environ.copy()
        environment["STORAGE_FILE_PATH"] = str(run / "hub-servers.json")
        start([sys.executable, "-c", "from scp.hub.hub_server import run; "
               f"run(host='127.0.0.1',port={hub_port})"], "hub.log", environment)
        with httpx.Client(timeout=5, trust_env=False) as client:
            for url in (f"http://127.0.0.1:{server_port}/sse", hub_url + "/tools"):
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
            response = client.post(hub_url + "/register_server", json={
                "server_name": "AMSTask", "server_description": "Independent AMS scientific task workspace",
                "server_url": "http://127.0.0.1", "server_port": server_port, "type": "sse",
            })
            response.raise_for_status()
            tools = client.get(hub_url + "/tools", params={"server_name": "AMSTask"})
            tools.raise_for_status()
            (run / "discovered-tools.json").write_text(json.dumps(tools.json(), indent=2))
        # Match the model-visible client cwd to the Server's task root. Tool
        # isolation still comes from the strict catalog and scientific sandbox.
        client_dir = task
        client_env = os.environ.copy()
        model_settings = {"env": {key: args.model for key in (
            "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
        )}}
        command = ["claude", "-p", prompt, "--model", args.model,
                   "--settings", json.dumps(model_settings), "--tools", "",
                   "--strict-mcp-config", "--mcp-config", str(run / "mcp.json"),
                   "--allowedTools", "mcp__scp__*", "--permission-mode", "dontAsk",
                   "--disable-slash-commands", "--no-session-persistence",
                   "--system-prompt", "You are a scientific analysis agent. Complete the user's task using the supplied tools. Call tools using their exact complete names from the current tool catalog, never shortened aliases from prose. Report only work you have executed and verified.",
                   "--output-format", "stream-json", "--verbose"]
        if args.budget > 0:
            command += ["--max-budget-usd", str(args.budget)]
        claude = start(command, "claude.jsonl", client_env, client_dir)
        record["model_started_at"] = time.time()
        record["client_cwd"] = str(client_dir)
        (run / "trial.json").write_text(json.dumps(record, indent=2))
        print(f"Trial running: {run}", flush=True)
        try:
            record["client_exit_code"] = claude.wait(timeout=args.minutes * 60)
        except subprocess.TimeoutExpired:
            claude.terminate()
            claude.wait(timeout=20)
            record["client_exit_code"] = claude.returncode
            record["stopped_reason"] = "wall_time_limit"
        print(f"Trial ended: {record}", flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for handle in handles:
            handle.close()
        transcript = run / "claude.jsonl"
        if transcript.exists():
            for line in transcript.read_text().splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    record["session_id"] = event.get("session_id")
                if event.get("type") == "result":
                    record["client_result"] = {key: event.get(key) for key in (
                        "terminal_reason", "subtype", "num_turns", "total_cost_usd", "modelUsage", "errors",
                    )}
        record["ended_at"] = time.time()
        (run / "trial.json").write_text(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
