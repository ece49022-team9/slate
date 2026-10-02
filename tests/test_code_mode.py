import asyncio
import json
import os
import signal
import sys
import unittest
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from slate.agent.code_mode import (
    DeviceClient,
    MontyExecutor,
    Runner,
    SandboxExecutor,
)
from slate.agent.device_mcp import build_server


def receipt(operation: str = "get_status", **values) -> dict:
    return {
        "request_id": "a" * 32,
        "operation": operation,
        "revision": 1,
        "state": 1,
        "color": "#112233",
        "radius": 24.0,
        "text": "Ready",
        "custom": True,
        **values,
    }


class DeviceFixture(unittest.IsolatedAsyncioTestCase):
    def setup_device(self, handler=None):
        self.requests: list[httpx.Request] = []

        def default(request):
            self.requests.append(request)
            name = request.url.path.rsplit("/", 1)[-1]
            if name == "status":
                name = "get_status"
            return httpx.Response(200, json=receipt(name))

        client = httpx.AsyncClient(
            base_url="http://device.test",
            transport=httpx.MockTransport(handler or default),
        )
        self.addAsyncCleanup(client.aclose)
        return DeviceClient(client)


class CodeModeTests(DeviceFixture):
    async def executor(self, device, **limits):
        executor = MontyExecutor(device, **limits)
        self.addAsyncCleanup(executor.close)
        return executor

    async def test_composed_program_returns_real_receipts_and_preserves_repl_values(
        self,
    ):
        device = self.setup_device()
        executor = await self.executor(device)
        result = await executor.execute(
            "scope-a",
            "orb = await device.set_orb('#112233', 24)\n"
            "text = await device.show_text('Ready')\n[orb, text]",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"], [receipt("set_orb"), receipt("show_text")])
        self.assertEqual(
            [call["receipt"] for call in result["calls"]], result["result"]
        )
        reused = await executor.execute("scope-a", "orb['revision']")
        self.assertEqual(reused["result"], 1)
        self.assertEqual(reused["calls"], [])
        self.assertEqual(
            [request.url.path for request in self.requests],
            [
                "/api/device/scope-a/status",
                "/api/device/scope-a/set_orb",
                "/api/device/scope-a/show_text",
                "/api/device/scope-a/status",
            ],
        )

    async def test_scope_change_discards_repl_state(self):
        executor = await self.executor(self.setup_device())
        await executor.execute("scope-a", "old = 123\nold")
        result = await executor.execute("scope-b", "old")
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["state_reset"])

    async def test_stale_scope_fails_even_for_pure_python_without_reusing_cached_state(
        self,
    ):
        active = True

        def handle(request):
            if not active:
                return httpx.Response(409, json={"detail": "Device scope is inactive"})
            return httpx.Response(200, json=receipt())

        executor = await self.executor(self.setup_device(handle))
        await executor.execute("scope-a", "saved = 1\nsaved")
        active = False
        result = await executor.execute("scope-a", "saved")
        self.assertEqual(result["status"], "error")
        self.assertIn("inactive", result["error"]["message"])
        self.assertEqual(result["calls"], [])
        self.assertTrue(result["state_reset"])

    async def test_bad_orb_and_text_arguments_never_reach_the_http_service(self):
        device = self.setup_device()
        for color, radius in (("blue", 24), ("#112233", 46), ("#112233", float("nan"))):
            with self.subTest(color=color, radius=radius):
                with self.assertRaises(ValueError):
                    await device.set_orb("scope-a", color, radius)
        for text in ("\n", "\u2603", "x" * 65):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    await device.show_text("scope-a", text)
        self.assertEqual(self.requests, [])

    async def test_mismatched_acknowledgment_is_rejected(self):
        device = self.setup_device(
            lambda request: httpx.Response(200, json=receipt("show_text"))
        )
        with self.assertRaisesRegex(ValueError, "operation does not match"):
            await device.set_orb("scope-a", "#112233", 24)

    async def test_partial_failure_preserves_acknowledged_actions_without_replay(self):
        writes = []

        def handle(request):
            name = request.url.path.rsplit("/", 1)[-1]
            if name == "show_text":
                return httpx.Response(409, json={"detail": "Device scope is inactive"})
            if name == "set_orb":
                writes.append(request)
            return httpx.Response(
                200, json=receipt("get_status" if name == "status" else name)
            )

        executor = await self.executor(self.setup_device(handle))
        result = await executor.execute(
            "scope-a",
            "await device.set_orb('#112233', 24)\nawait device.show_text('Ready')",
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(
            [call["status"] for call in result["calls"]], ["completed", "failed"]
        )
        self.assertEqual(result["calls"][0]["receipt"], receipt("set_orb"))
        self.assertIn("inactive", result["error"]["message"])
        self.assertEqual(len(writes), 1)
        self.assertTrue(result["state_reset"])

    async def test_parallel_calls_share_the_call_budget(self):
        executor = await self.executor(self.setup_device(), max_calls=2)
        result = await executor.execute(
            "scope-a",
            "import asyncio\n"
            "await asyncio.gather(device.get_status(), device.get_status())",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"], [receipt(), receipt()])
        self.assertEqual(len(result["calls"]), 2)

    async def test_paths_and_private_host_methods_are_not_capabilities(self):
        executor = await self.executor(self.setup_device())
        for code in (
            "open('/etc/passwd').read()",
            "import os\nos.getenv('HOME')",
            "device.client",
            "await device._call('DELETE', '/api/device/scope-b/status')",
            "device.get_status.__globals__",
        ):
            with self.subTest(code=code):
                result = await executor.execute("scope-a", code)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["calls"], [])

    async def test_call_budget_stops_loop_and_retains_receipts_of_completed_actions(
        self,
    ):
        executor = await self.executor(self.setup_device(), max_calls=2)
        result = await executor.execute(
            "scope-a", "for index in range(5):\n    await device.show_text(str(index))"
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(result["calls"]), 2)
        self.assertTrue(all(call["status"] == "completed" for call in result["calls"]))
        self.assertTrue(result["state_reset"])

    async def test_cpu_memory_print_and_result_limits_fail_without_device_work(self):
        executor = await self.executor(
            self.setup_device(), max_duration=0.1, max_memory=2_000_000, max_output=1024
        )
        for code in (
            "while True:\n    pass",
            "'x' * 3_000_000",
            "print('x' * 2000)",
            "'x' * 2000",
        ):
            with self.subTest(code=code):
                async with asyncio.timeout(3):
                    result = await executor.execute("scope-a", code)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["calls"], [])

    async def test_cancellation_stops_pending_http_call_and_resets_session(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handle(request):
            if request.url.path.endswith("/set_orb"):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return httpx.Response(200, json=receipt())

        executor = await self.executor(self.setup_device(handle))
        task = asyncio.create_task(
            executor.execute("scope-a", "await device.set_orb('#112233', 24)")
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertEqual((await executor.execute("scope-a", "1 + 1"))["result"], 2)

    async def test_mcp_modes_expose_one_execute_tool_or_three_typed_baseline_tools(
        self,
    ):
        for mode, names in (
            ("tools", {"device_set_orb", "device_show_text", "device_get_status"}),
            ("monty", {"execute_device_code"}),
            ("modal", {"execute_device_code"}),
        ):
            with self.subTest(mode=mode):
                server = build_server(mode=mode)
                tools = await server.list_tools()
                self.assertEqual({tool.name for tool in tools}, names)
                for tool in tools:
                    self.assertIn("scope", tool.input_schema["properties"])


class LocalDeviceHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = Path(__file__).resolve().parents[1]
        self.requests = []
        self.active = True
        self.entered = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.block_orb = False

        async def handle(reader, writer):
            try:
                header = (await reader.readuntil(b"\r\n\r\n")).decode()
                path = header.split(" ")[1]
                name = path.rsplit("/", 1)[-1]
                self.requests.append(name)
                length = next(
                    (
                        int(line.split(":")[1])
                        for line in header.splitlines()
                        if line.lower().startswith("content-length:")
                    ),
                    0,
                )
                if length:
                    await reader.readexactly(length)
                if self.block_orb and name == "set_orb":
                    self.entered.set()
                    await reader.read()
                    self.disconnected.set()
                    return
                body = json.dumps(
                    receipt("get_status" if name == "status" else name)
                    if self.active
                    else {"detail": "Device scope is inactive"}
                ).encode()
                status = b"200 OK" if self.active else b"409 Conflict"
                writer.write(
                    b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                    b"Connection: close\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.device_server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.addAsyncCleanup(self.close_device)
        port = self.device_server.sockets[0].getsockname()[1]
        self.device_url = f"http://127.0.0.1:{port}"

    async def close_device(self):
        self.device_server.close()
        await self.device_server.wait_closed()


class StdioMCPTests(LocalDeviceHTTPTests):
    async def test_real_stdio_monty_and_baseline_share_firmware_receipts(self):
        for mode in ("monty", "tools"):
            with self.subTest(mode=mode):
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "slate.agent.device_mcp"],
                    cwd=self.repo,
                    env={
                        **os.environ,
                        "SLATE_DEVICE_MODE": mode,
                        "SLATE_DEVICE_URL": self.device_url,
                    },
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        if mode == "monty":
                            result = await session.call_tool(
                                "execute_device_code",
                                {
                                    "scope": "scope-a",
                                    "code": "await device.set_orb('#112233', 24)",
                                },
                            )
                            value = result.structured_content["result"]
                        else:
                            result = await session.call_tool(
                                "device_set_orb",
                                {"scope": "scope-a", "color": "#112233", "radius": 24},
                            )
                            value = result.structured_content
                        self.assertFalse(result.is_error)
                        self.assertEqual(value, receipt("set_orb"))


RUNNER = Path(__file__).resolve().parents[1] / "server/slate/agent/sandbox_runner.py"


class LocalSandbox:
    def __init__(self) -> None:
        self.processes: list[asyncio.subprocess.Process] = []
        self.terminated = 0

    async def spawn(self) -> Runner:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            str(RUNNER),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        self.processes.append(process)

        async def chunks():
            while chunk := await process.stdout.read(4096):
                yield chunk

        async def close():
            process.stdin.close()
            await process.wait()

        return Runner(
            lambda text: process.stdin.write(text.encode()),
            process.stdin.drain,
            chunks(),
            close,
            65_536,
        )

    async def terminate(self) -> None:
        self.terminated += 1
        for process in self.processes:
            if process.returncode is None:
                process.send_signal(signal.SIGKILL)
                await process.wait()


class SandboxExecutorTests(DeviceFixture):
    async def executor(self, device, **limits):
        self.sandbox = LocalSandbox()
        executor = SandboxExecutor(device, self.sandbox, **limits)
        self.addAsyncCleanup(executor.close)
        return executor

    async def test_cpython_program_composes_sdk_and_keeps_scope_state(self):
        executor = await self.executor(self.setup_device())
        first = await executor.execute(
            "scope-a",
            "import asyncio, statistics\n"
            "orb, text = await asyncio.gather(\n"
            "    device.set_orb('#112233', 24), device.show_text('Ready'))\n"
            "print('sent', statistics.mean([1, 3]))\n"
            "[orb['operation'], text['operation']]",
        )
        self.assertEqual(first["status"], "completed", first)
        self.assertEqual(first["result"], ["set_orb", "show_text"])
        self.assertEqual(first["output"], "sent 2\n")
        self.assertEqual(
            [call["status"] for call in first["calls"]], ["completed", "completed"]
        )
        kept = await executor.execute("scope-a", "orb['revision']")
        self.assertEqual(kept["result"], 1)
        fresh = await executor.execute("scope-b", "orb")
        self.assertEqual(fresh["error"]["type"], "NameError")
        self.assertEqual(len(self.sandbox.processes), 2)
        self.assertEqual(self.sandbox.terminated, 0)

    async def test_device_errors_are_python_exceptions_and_failures_reset_state(self):
        executor = await self.executor(self.setup_device())
        caught = await executor.execute(
            "scope-a",
            "value = 7\n"
            "try:\n    await device.set_orb('blue', 99)\n"
            "except Exception as error:\n    message = str(error)\n"
            "message",
        )
        self.assertEqual(caught["status"], "completed")
        self.assertIn("ValidationError", caught["result"])
        self.assertEqual(caught["calls"][0]["status"], "failed")
        self.assertEqual(
            [request.url.path for request in self.requests],
            ["/api/device/scope-a/status"],
        )
        failure = await executor.execute("scope-a", "raise KeyError('boom')")
        self.assertEqual(failure["error"]["type"], "KeyError")
        self.assertTrue(failure["state_reset"])
        reset = await executor.execute("scope-a", "value")
        self.assertEqual(reset["error"]["type"], "NameError")
        self.assertEqual(self.sandbox.terminated, 0)

    async def test_runaway_program_discards_the_sandbox_and_next_run_works(self):
        executor = await self.executor(self.setup_device(), timeout=1)
        async with asyncio.timeout(5):
            stuck = await executor.execute("scope-a", "while True:\n    pass")
        self.assertEqual(stuck["error"]["type"], "TimeoutError")
        self.assertEqual(self.sandbox.terminated, 1)
        self.assertEqual((await executor.execute("scope-a", "6 * 7"))["result"], 42)

    async def test_call_budget_stops_the_program_after_acknowledged_calls(self):
        executor = await self.executor(self.setup_device(), max_calls=2)
        result = await executor.execute(
            "scope-a", "for _ in range(5):\n    await device.show_text('Ready')"
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("budget", result["error"]["message"])
        self.assertEqual(
            [call["status"] for call in result["calls"]], ["completed", "completed"]
        )

    async def test_inactive_scope_runs_no_code(self):
        def inactive(request):
            self.requests.append(request)
            return httpx.Response(409, json={"detail": "This device turn has ended"})

        executor = await self.executor(self.setup_device(inactive))
        result = await executor.execute("scope-a", "await device.set_orb('#112233')")
        self.assertEqual(result["status"], "error")
        self.assertIn("ended", result["error"]["message"])
        self.assertEqual(result["calls"], [])
        self.assertEqual(self.sandbox.processes, [])

    async def test_cancellation_stops_pending_call_and_discards_sandbox(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handle(request):
            if request.url.path.endswith("/set_orb"):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return httpx.Response(200, json=receipt())

        executor = await self.executor(self.setup_device(handle))
        task = asyncio.create_task(
            executor.execute("scope-a", "await device.set_orb('#112233', 24)")
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.sandbox.terminated, 1)
        self.assertEqual((await executor.execute("scope-a", "1 + 1"))["result"], 2)


if __name__ == "__main__":
    unittest.main()
