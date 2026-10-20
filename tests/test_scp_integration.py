"""Opt-in protocol test with the real SDK, Hub and scientific runtime."""

import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get("AMS_FLUX_INTEGRATION") == "1", "Set AMS_FLUX_INTEGRATION=1 for ROOT/SSE/Hub checks")
class ProtocolTests(unittest.TestCase):
    def test_analysis_restart_validation_results_and_cancellation(self):
        import httpx
        from scp import ClientSession
        from scp.client.sse import sse_client

        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="ams-scp-integration-") as temporary:
            work = Path(temporary)
            self.work = work

            def port():
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    return listener.getsockname()[1]

            server_port = port()
            url = f"http://127.0.0.1:{server_port}/sse"
            processes = []
            handles = []

            def start(arguments, name, environment=None):
                output = (work / name).open("a")
                handles.append(output)
                process = subprocess.Popen(arguments, cwd=project, env=environment,
                                           stdout=output, stderr=subprocess.STDOUT)
                processes.append(process)
                return process

            def start_server():
                return start([sys.executable, "-B", "-m", "ams_flux_server.server",
                              "--runs", str(work / "runs"), "--port", str(server_port)], "server.log")

            async def call(name, arguments=None):
                async with sse_client(url) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments or {})
                        self.assertFalse(result.isError, result.content)
                        if result.content[0].type == "image":
                            return result.content[0]
                        return json.loads(result.content[0].text)

            async def ready(process):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail((work / "server.log").read_text())
                    try:
                        await call("describe_analysis")
                        return
                    except Exception:
                        await asyncio.sleep(.2)
                self.fail("SCP service did not start")

            async def wait_job(job_id):
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    state = await call("get_status", {"job_id": job_id})
                    if state["status"] in {"failed", "cancelled", "succeeded"}:
                        self.assertEqual(state["status"], "succeeded", state)
                        return state
                    await asyncio.sleep(1)
                self.fail(f"Job timed out: {job_id}")

            async def exercise():
                server = start_server()
                await ready(server)
                async with sse_client(url) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        self.assertEqual({tool.name for tool in listed.tools}, {
                            "describe_analysis", "run_analysis", "get_status", "get_result",
                            "validate_job", "cancel_job",
                        })
                        invalid = await session.call_tool("get_status", {"job_id": "../outside"})
                        self.assertTrue(invalid.isError)
                start_time = time.monotonic()
                analysis = await call("run_analysis")
                self.assertLess(time.monotonic() - start_time, 10)
                # A new service process must recover the detached job after disconnect.
                server.terminate()
                server.wait(timeout=10)
                server = start_server()
                await ready(server)
                state = await wait_job(analysis["job_id"])
                self.assertEqual(state["exit_code"], 0)
                spectrum = await call("get_result", {"job_id": analysis["job_id"]})
                self.assertEqual(len(spectrum["energy_edges"]), 15)
                self.assertEqual(len(spectrum["flux"]["values"]), 14)
                self.assertEqual(len(spectrum["flux"]["covariance"]), 14)
                self.assertTrue(all(value > 0 for value in spectrum["flux"]["values"]))
                report = await call("get_result", {"job_id": analysis["job_id"], "artifact": "report"})
                self.assertIn("Final", report["text"])
                binary = await call("get_result", {"job_id": analysis["job_id"], "artifact": "root"})
                self.assertTrue(base64.b64decode(binary["data"]).startswith(b"root"))
                for artifact in ("spectrum_plot", "spectrum_e3_plot"):
                    picture = await call("get_result", {"job_id": analysis["job_id"], "artifact": artifact})
                    self.assertEqual(picture.mimeType, "image/png")
                    self.assertTrue(base64.b64decode(picture.data).startswith(b"\x89PNG"))

                # Exercise the actual Hub HTTP routes, not a substitute router.
                hub_port = port()
                environment = os.environ.copy()
                environment["STORAGE_FILE_PATH"] = str(work / "hub-servers.json")
                hub = start([sys.executable, "-B", "-c",
                             "from scp.hub.hub_server import run; "
                             f"run(host='127.0.0.1', port={hub_port})"], "hub.log", environment)
                hub_url = f"http://127.0.0.1:{hub_port}"
                async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            response = await client.get(hub_url + "/tools?server_name=probe")
                            if response.status_code == 404:
                                break
                        except httpx.ConnectError:
                            pass
                        if time.monotonic() > deadline or hub.poll() is not None:
                            self.fail((work / "hub.log").read_text())
                        await asyncio.sleep(.2)
                    response = await client.post(hub_url + "/register_server", json={
                        "server_name": "AMSFlux", "server_description": "AMS flux analysis",
                        "server_url": "http://127.0.0.1", "server_port": server_port, "type": "sse",
                    })
                    self.assertEqual(response.status_code, 201, response.text)
                    response = await client.get(hub_url + "/tools?server_name=AMSFlux")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("AMSFlux.run_analysis", {tool["name"] for tool in response.json()})
                    response = await client.post(hub_url + "/tools/call_tool", json={
                        "name": "AMSFlux.get_result", "job_id": analysis["job_id"], "artifact": "spectrum",
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(json.loads(response.json()[0]["text"]), spectrum)
                    response = await client.post(hub_url + "/tools/call_tool", json={
                        "name": "AMSFlux.validate_job", "job_id": analysis["job_id"], "replay": True,
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    validation = json.loads(response.json()[0]["text"])
                await wait_job(validation["job_id"])
                accepted = await call("get_result", {"job_id": validation["job_id"], "artifact": "acceptance"})
                self.assertEqual(accepted["status"], "PASS")
                self.assertEqual(accepted["valid_bins"], 14)
                self.assertEqual(accepted["independent_uncertainty_execution"], "PASS")
                self.assertTrue(accepted["final_sources_match_both_clean_replays"])
                self.assertEqual([item["status"] for item in accepted["clean_replays"]], ["PASS", "PASS"])
                cancelled = await call("run_analysis")
                result = await call("cancel_job", {"job_id": cancelled["job_id"]})
                self.assertEqual(result["status"], "cancelled", result)

            try:
                asyncio.run(exercise())
            finally:
                # Workers outlive the service; explicitly cancel any unfinished fixture job.
                from ams_flux_server.jobs import JobManager
                manager = JobManager(project, runs=work / "runs")
                for directory in manager.runs.glob("[0-9a-f]" * 32):
                    manager.cancel(directory.name)
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
                for handle in handles:
                    handle.close()


if __name__ == "__main__":
    unittest.main()
