import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from slate.agent.code_mode import DeviceClient, MontyExecutor
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


class CodeModeTests(unittest.IsolatedAsyncioTestCase):
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
            ("cloudflare", {"execute_device_code"}),
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


class CloudflareRuntimeTests(LocalDeviceHTTPTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        if not (
            self.repo / ".local/cloudflare-code-mode/node_modules/miniflare"
        ).is_dir():
            self.skipTest(
                "Install the pinned local Cloudflare prototype dependencies first"
            )
        self.process = await asyncio.create_subprocess_exec(
            "node",
            str(self.repo / "scripts/cloudflare_code_mode.mjs"),
            env={
                **os.environ,
                "SLATE_DEVICE_URL": self.device_url,
                "SLATE_CLOUDFLARE_CODE_PORT": "0",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.addAsyncCleanup(self.close_runtime)
        line = await asyncio.wait_for(self.process.stdout.readline(), 5)
        self.assertIn(b"listening", line)
        runtime_port = int(line.rsplit(b":", 1)[1])
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{runtime_port}", timeout=10
        )
        self.addAsyncCleanup(self.client.aclose)

    async def close_runtime(self):
        if self.process.returncode is None:
            self.process.terminate()
        await asyncio.wait_for(self.process.communicate(), 5)

    async def execute(self, code):
        response = await self.client.post(
            "/execute", json={"scope": "scope-a", "code": code}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_real_workers_sdk_composition_network_denial_and_stale_scope(self):
        result = await self.execute(
            "async () => { return [await device.set_orb('#112233', 24), "
            "await device.show_text('Ready')]; }"
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"], [receipt("set_orb"), receipt("show_text")])
        self.assertEqual(self.requests, ["status", "set_orb", "show_text"])
        denied = await self.execute("async () => await fetch('https://example.com')")
        self.assertEqual(denied["status"], "error")
        self.assertIn("not permitted", denied["error"]["message"])
        self.assertEqual(denied["calls"], [])
        unknown = await self.execute("async () => await device.delete_all()")
        self.assertEqual(unknown["status"], "error")
        self.assertEqual(unknown["calls"], [])
        self.active = False
        stale = await self.execute("async () => 42")
        self.assertEqual(stale["status"], "error")
        self.assertIn("inactive", stale["error"]["message"])

    async def test_cpu_loop_is_stopped_by_external_watchdog_and_next_execution_works(
        self,
    ):
        async with asyncio.timeout(8):
            result = await self.execute("async () => { while (true) {} }")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["type"], "ResourceLimit")
        self.assertFalse(result["calls_available"])
        self.assertIn("do not replay", result["warning"])
        self.assertEqual((await self.execute("async () => 2"))["result"], 2)

    async def test_worker_arguments_call_budget_and_output_limits(self):
        invalid = await self.execute("async () => await device.set_orb('blue', 99)")
        self.assertEqual(invalid["status"], "error")
        self.assertEqual(invalid["calls"], [])
        self.assertEqual(self.requests, ["status"])
        exhausted = await self.execute(
            "async () => { for (let i = 0; i < 15; i++) "
            "await device.show_text('Ready'); }"
        )
        self.assertEqual(exhausted["status"], "error")
        self.assertEqual(len(exhausted["calls"]), 12)
        self.assertTrue(
            all(call["status"] == "completed" for call in exhausted["calls"])
        )
        for code in (
            "async () => 'x'.repeat(20000)",
            "async () => { console.log('x'.repeat(20000)); return 1; }",
        ):
            with self.subTest(code=code):
                result = await self.execute(code)
                self.assertEqual(result["status"], "error")
                self.assertIn("output limit", result["error"]["message"])

    async def test_client_cancellation_closes_the_pending_device_http_connection(self):
        self.block_orb = True
        task = asyncio.create_task(
            self.execute("async () => await device.set_orb('#112233', 24)")
        )
        try:
            await asyncio.wait_for(self.entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(self.disconnected.wait(), 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
