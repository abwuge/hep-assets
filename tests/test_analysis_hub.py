"""Analyze user-supplied CSV through MCP -> official Hub -> analysis service."""

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

import httpx
from scp import ClientSession
from scp.client.streamable_http import streamablehttp_client
from scp.types import TextContent

PROJECT = Path(__file__).resolve().parents[1]
ANALYSIS_CODE = """
import csv, json
from pathlib import Path
rows = list(csv.DictReader(Path('inputs/measurements.csv').open()))
x = [float(row['energy_GeV']) for row in rows]
y = [float(row['counts']) for row in rows]
assert len(x) == len(y) and len(x) > 1
mx, my = sum(x) / len(x), sum(y) / len(y)
slope = sum((a-mx)*(b-my) for a,b in zip(x,y)) / sum((a-mx)**2 for a in x)
intercept = my - slope * mx
residuals = [b - (slope*a + intercept) for a,b in zip(x,y)]
result = dict(rows=len(x), slope=slope, intercept=intercept, residuals=residuals)
Path('outputs/fit.json').write_text(json.dumps(result))
Path('outputs/report.md').write_text(
    f'# Linear fit\\nRows: {len(x)}\\nSlope: {slope} counts/GeV\\n'
    f'Intercept: {intercept} counts\\nResiduals: {residuals}\\n')
points = ' '.join(f'{40 + a*8},{220 - b*2}' for a,b in zip(x,y))
Path('outputs/fit.svg').write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="280">'
    '<rect width="400" height="280" fill="white"/>'
    '<path d="M40 20V220H370" fill="none" stroke="black"/>'
    f'<polyline points="{points}" fill="none" stroke="#145c70" stroke-width="2"/>'
    '<text x="140" y="260">Energy [GeV]</text>'
    '<text x="6" y="16">Counts</text></svg>')
saved = json.loads(Path('outputs/fit.json').read_text())
assert saved['rows'] == len(rows) and all(abs(r) < 1e-12 for r in saved['residuals'])
print('Analysis and output readback passed')
"""


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(sys.platform in {"linux", "darwin"}, "Requires Linux or macOS")
class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    def start_service(self, args: list[str], port: int, env: dict[str, str]):
        log = tempfile.TemporaryFile(mode="w+t")
        self.addCleanup(log.close)
        process = subprocess.Popen(
            [sys.executable, *args], cwd=PROJECT, env=env, stdout=log, stderr=log
        )

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        self.addCleanup(stop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return process
            except OSError:
                time.sleep(0.1)
        log.seek(0)
        self.fail(f"Service failed to start:\n{log.read()}")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.port, hub_port = free_port(), free_port()
        self.hub = f"http://127.0.0.1:{hub_port}"
        self.env = {**os.environ, "STORAGE_FILE_PATH": str(self.root / "servers.json")}
        self.assets = self.root / "assets"
        (self.assets / "data/demo_flux").mkdir(parents=True)
        (self.assets / "knowledge/demo_flux").mkdir(parents=True)
        (self.assets / "data/demo_flux/measurements.csv").write_text(
            "energy_GeV,counts\n10,21\n20,41\n30,61\n"
        )
        (self.assets / "knowledge/demo_flux/method.md").write_text(
            "Fit a straight line."
        )
        self.mcp_port = free_port()
        self.mcp = f"http://127.0.0.1:{self.mcp_port}/mcp"
        self.start_service(
            [
                "-m",
                "hub",
                "--hub-port",
                str(hub_port),
                "--port",
                str(self.mcp_port),
                "--storage",
                str(self.root / "hub"),
            ],
            self.mcp_port,
            self.env,
        )
        self.server_args = [
            "-m",
            "analysis",
            "--storage",
            str(self.root / "tasks"),
            "--assets",
            str(self.assets),
            "--port",
            str(self.port),
            "--hub",
            self.hub,
        ]
        self.process = self.start_service(self.server_args, self.port, self.env)
        with httpx.Client(trust_env=False) as client:
            deadline = time.monotonic() + 10
            while True:
                response = client.get(
                    self.hub + "/tools", params={"server_name": "HEPAnalysis"}
                )
                if response.status_code != 404:
                    response.raise_for_status()
                    if any(
                        item["name"] == "HEPAnalysis.create_task"
                        for item in response.json()
                    ):
                        break
                if time.monotonic() > deadline:
                    self.fail("Analysis service did not register with Hub")
                time.sleep(0.1)

    async def test_user_data_to_verified_results_and_server_recovery(self):
        async with (
            streamablehttp_client(self.mcp) as (read, write, _),
            ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=15)
            ) as session,
        ):
            await session.initialize()

            async def call(tool_name: str, **arguments) -> dict:
                result = await session.call_tool("HEPAnalysis." + tool_name, arguments)
                self.assertFalse(result.isError, result.content)
                block = result.content[0]
                assert isinstance(block, TextContent)
                value = json.loads(block.text)
                self.assertTrue(value["ok"], value)
                return value

            catalog = {item.name for item in (await session.list_tools()).tools}
            self.assertIn("HEPAnalysis.create_task", catalog)
            self.assertIn("HEPAnalysis.create_upload", catalog)
            self.assertIn("HEPAnalysis.result_link", catalog)
            info = await call("analysis_info")
            self.assertTrue(info["data_transfer"]["http_available"])
            self.assertEqual(
                info["data_transfer"]["http_base_url"], f"http://127.0.0.1:{self.port}"
            )
            task = await call(
                "create_task",
                request="Fit counts against energy; return the fit, plot and report.",
            )
            task_id = task["task_id"]
            await call(
                "upload_input",
                task_id=task_id,
                input_name="measurements.csv",
                data_base64=base64.b64encode(
                    b"energy_GeV,counts\n10,21\n20,41\n30,61\n"
                ).decode("ascii"),
            )
            inputs = await call("list_files", task_id=task_id, path="inputs")
            self.assertEqual(inputs["files"][0]["path"], "inputs/measurements.csv")
            preview = await call(
                "read_file", task_id=task_id, path="inputs/measurements.csv"
            )
            self.assertIn("energy_GeV", preview["text"])
            await call(
                "write_file",
                task_id=task_id,
                path="work/analyze.py",
                content=ANALYSIS_CODE,
            )
            command = await call(
                "execute",
                task_id=task_id,
                command="sleep 3; /usr/bin/python3 work/analyze.py",
            )
            self.process.terminate()
            self.process.wait(timeout=5)
            self.process = self.start_service(self.server_args, self.port, self.env)
            for _ in range(20):
                state = await call(
                    "poll_command",
                    task_id=task_id,
                    command_id=command["command_id"],
                    wait_seconds=1,
                )
                if state["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(state["status"], "succeeded", state)
            fit = await call("read_file", task_id=task_id, path="outputs/fit.json")
            values = json.loads(fit["text"])
            self.assertEqual(
                values,
                {
                    "rows": 3,
                    "slope": 2.0,
                    "intercept": 1.0,
                    "residuals": [0.0, 0.0, 0.0],
                },
            )
            completed = await call(
                "complete_task",
                task_id=task_id,
                summary=(
                    "Slope 2 counts/GeV, intercept 1 count; "
                    "output readback and residual checks passed."
                ),
                artifacts=["outputs/fit.json", "outputs/report.md", "outputs/fit.svg"],
            )
            self.assertEqual(completed["status"], "completed")
            chunks, offset = [], 0
            while True:
                chunk = await call(
                    "download_file",
                    task_id=task_id,
                    path="outputs/fit.json",
                    offset=offset,
                    limit=37,
                )
                chunks.append(base64.b64decode(chunk["data"]))
                offset = chunk["next_offset"]
                if not chunk["has_more"]:
                    break
            self.assertEqual(json.loads(b"".join(chunks)), values)
            restored = await call("get_task", task_id=task_id)
            self.assertEqual(
                restored["completion"]["summary"], completed["completion"]["summary"]
            )
            invalid = await session.call_tool(
                "HEPAnalysis.read_file", {"task_id": task_id, "path": "../task.json"}
            )
            block = invalid.content[0]
            assert isinstance(block, TextContent)
            self.assertFalse(json.loads(block.text)["ok"])

    async def test_http_targets_via_hub_and_raw_bytes_after_restart(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.process = self.start_service(self.server_args, self.port, self.env)
        async with (
            streamablehttp_client(self.mcp) as (read, write, _),
            ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=15)
            ) as session,
            httpx.AsyncClient(trust_env=False) as http,
        ):
            await session.initialize()

            async def call(name, **arguments):
                response = await session.call_tool("HEPAnalysis." + name, arguments)
                self.assertFalse(response.isError)
                block = response.content[0]
                assert isinstance(block, TextContent)
                result = json.loads(block.text)
                self.assertTrue(result["ok"], result)
                return result

            info = await call("analysis_info")
            self.assertTrue(info["data_transfer"]["http_available"])
            task_id = (await call("create_task", request="Receive binary data"))[
                "task_id"
            ]
            upload = await call("create_upload", task_id=task_id, input_name="data.bin")
            self.process.terminate()
            self.process.wait(timeout=5)
            self.process = self.start_service(self.server_args, self.port, self.env)
            data = bytes(range(256)) * 4096
            response = await http.put(upload["url"], content=data)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["bytes"], len(data))
            chunk = await call(
                "download_file", task_id=task_id, path="inputs/data.bin", limit=256
            )
            self.assertEqual(base64.b64decode(chunk["data"]), data[:256])
            await call(
                "write_file",
                task_id=task_id,
                path="outputs/report.txt",
                content="verified result",
            )
            download = await call(
                "result_link", task_id=task_id, path="outputs/report.txt"
            )
            response = await http.get(download["url"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"verified result")
            response = await http.get(upload["url"])
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["content-type"], "application/json")

    async def test_uploaded_task_uses_server_assets_read_only(self):
        async with (
            streamablehttp_client(self.mcp) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            async def call(name, **arguments):
                result = await session.call_tool("HEPAnalysis." + name, arguments)
                block = result.content[0]
                assert isinstance(block, TextContent)
                value = json.loads(block.text)
                self.assertTrue(value["ok"], value)
                return value

            info = await call("analysis_info")
            self.assertTrue(info["assets"]["available"])
            task_id = (await call("create_task", request="Solve the uploaded task"))[
                "task_id"
            ]
            await call(
                "upload_input",
                task_id=task_id,
                input_name="TASK.md",
                data_base64=base64.b64encode(b"Fit the shared measurements.").decode(),
            )
            files = await call("list_files", task_id=task_id, path="assets")
            self.assertEqual(len(files["files"]), 2)
            method = await call(
                "read_file",
                task_id=task_id,
                path="assets/knowledge/demo_flux/method.md",
            )
            self.assertEqual(method["text"], "Fit a straight line.")
            denied = await session.call_tool(
                "HEPAnalysis.write_file",
                {
                    "task_id": task_id,
                    "path": "assets/knowledge/demo_flux/method.md",
                    "content": "changed",
                },
            )
            block = denied.content[0]
            assert isinstance(block, TextContent)
            self.assertFalse(json.loads(block.text)["ok"])
            code = """
import os
from pathlib import Path
assets = Path(os.environ['ASSETS_DIR'])
assert Path('inputs/TASK.md').is_file()
method = assets / 'knowledge/demo_flux/method.md'
assert method.read_text() == 'Fit a straight line.'
target = assets / 'data/demo_flux/measurements.csv'
for operation in (
    lambda: target.write_text('bad'),
    lambda: target.unlink(),
    lambda: target.rename(assets / 'moved.csv'),
    lambda: (assets / 'new.txt').write_text('bad'),
    lambda: os.symlink('data', assets / 'link'),
):
    try: operation()
    except OSError: pass
    else: raise AssertionError('assets writable')
try:
    os.link(target, 'work/asset-hardlink')
except OSError: pass
else:
    try: Path('work/asset-hardlink').write_text('bad')
    except OSError: pass
    else: raise AssertionError('asset writable through hard link')
""" + ANALYSIS_CODE.replace(
                "Path('inputs/measurements.csv')",
                "(assets / 'data/demo_flux/measurements.csv')",
            )
            await call(
                "write_file", task_id=task_id, path="work/solve.py", content=code
            )
            command = await call(
                "execute", task_id=task_id, command="/usr/bin/python3 work/solve.py"
            )
            for _ in range(20):
                state = await call(
                    "poll_command",
                    task_id=task_id,
                    command_id=command["command_id"],
                    wait_seconds=1,
                )
                if state["status"] in {"succeeded", "failed", "cancelled"}:
                    break
            self.assertEqual(state["status"], "succeeded", state)
            result = await call("read_file", task_id=task_id, path="outputs/fit.json")
            self.assertEqual(json.loads(result["text"])["slope"], 2)
            self.assertEqual(
                (self.assets / "data/demo_flux/measurements.csv").read_text(),
                "energy_GeV,counts\n10,21\n20,41\n30,61\n",
            )


if __name__ == "__main__":
    unittest.main()
