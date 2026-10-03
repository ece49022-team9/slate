import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import modal
from pydantic_monty import AsyncMonty, ClassInstance, CollectStreams

from slate.device import DeviceStatus, OrbRequest, TextRequest

logger = logging.getLogger("slate.agent.code_mode")
RUNNER_PATH = "/opt/slate/runner.py"
SANDBOX_IMAGE = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install("httpx==0.28.1")
    .add_local_file(Path(__file__).with_name("sandbox_runner.py"), RUNNER_PATH)
)
DEVICE_STUBS = """from typing import TypedDict
class DeviceReceipt(TypedDict):
    request_id: str
    operation: str
    revision: int
    state: int
    color: str
    radius: float
    text: str
    custom: bool
class ScopedDevice:
    async def set_orb(self, color: str, radius: float = 24) -> DeviceReceipt: ...
    async def show_text(self, text: str) -> DeviceReceipt: ...
    async def get_status(self) -> DeviceReceipt: ...
device: ScopedDevice
"""


class DeviceClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _request(
        self, scope: str, operation: str, body: dict | None = None
    ) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scope):
            raise ValueError("Device scope must be an opaque capability identifier")
        response = await self.client.request(
            "GET" if body is None else "POST",
            f"/api/device/{operation}",
            headers={"Authorization": f"Bearer {scope}"},
            **({"json": body} if body is not None else {}),
            timeout=3,
        )
        if len(response.content) > 16_384:
            raise ValueError("Device response exceeds the output limit")
        if response.is_error:
            detail = response.json().get("detail", "Device request failed")
            raise RuntimeError(f"Device HTTP {response.status_code}: {detail}")
        receipt = DeviceStatus.model_validate(response.json()).model_dump(mode="json")
        if receipt["operation"] != (
            "get_status" if operation == "status" else operation
        ):
            raise ValueError(
                "Device acknowledgment operation does not match the request"
            )
        return receipt

    async def set_orb(self, scope: str, color: str, radius: float = 24) -> dict:
        request = OrbRequest(color=color, radius=radius)
        return await self._request(scope, "set_orb", request.model_dump(mode="json"))

    async def show_text(self, scope: str, text: str) -> dict:
        request = TextRequest(text=text)
        return await self._request(scope, "show_text", request.model_dump(mode="json"))

    async def get_status(self, scope: str) -> dict:
        return await self._request(scope, "status")


class ScopedDevice:
    def __init__(self, device: DeviceClient, scope: str, max_calls: int) -> None:
        self.device = device
        self.scope = scope
        self.max_calls = max_calls
        self.calls: list[dict] = []
        self.active = False
        self.pending: set[asyncio.Task] = set()

    async def revoke(self) -> None:
        self.active = False
        pending = tuple(self.pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _call(self, name: str, arguments: dict) -> dict:
        if not self.active:
            raise RuntimeError("This device execution has ended")
        if len(self.calls) >= self.max_calls:
            raise RuntimeError("Device tool-call budget exceeded")
        call = {"tool": name, "arguments": arguments, "status": "started"}
        self.calls.append(call)
        try:
            if name == "set_orb":
                operation = self.device.set_orb(self.scope, **arguments)
            elif name == "show_text":
                operation = self.device.show_text(self.scope, **arguments)
            elif name == "get_status":
                operation = self.device.get_status(self.scope)
            else:
                raise ValueError("Unknown device capability")
            task = asyncio.create_task(operation)
            self.pending.add(task)
            try:
                result = await task
            finally:
                self.pending.discard(task)
        except BaseException as error:
            call.update(
                status="unfinished"
                if isinstance(error, asyncio.CancelledError)
                else "failed",
                error_type=type(error).__name__,
            )
            raise
        call.update(status="completed", receipt=result)
        return result

    async def set_orb(self, color: str, radius: float = 24) -> dict:
        return await self._call("set_orb", {"color": color, "radius": radius})

    async def show_text(self, text: str) -> dict:
        return await self._call("show_text", {"text": text})

    async def get_status(self) -> dict:
        return await self._call("get_status", {})


def validate_code(code: str) -> None:
    if not code.strip() or len(code.encode()) > 16_384:
        raise ValueError("Code must contain 1..16384 UTF-8 bytes")


def completed(value, output: str, calls: list, scope_status: dict, limit: int) -> dict:
    if len(json.dumps(value, allow_nan=False).encode()) > limit:
        raise ValueError("Code result exceeds the output limit")
    if len(output.encode()) > limit:
        raise ValueError("Printed output exceeds the output limit")
    return {
        "status": "completed",
        "result": value,
        "output": output,
        "calls": calls,
        "scope_status": scope_status,
        "state_reset": False,
    }


def failed(kind: str, message: str, output: str, calls: list, limit: int) -> dict:
    logger.warning("Device code failed: error=%s", kind)
    return {
        "status": "error",
        "error": {
            "type": kind,
            "message": message.encode()[:limit].decode(errors="ignore"),
        },
        "output": output.encode()[:limit].decode(errors="ignore"),
        "calls": calls,
        "state_reset": True,
    }


class MontyExecutor:
    def __init__(
        self,
        device: DeviceClient,
        *,
        max_calls: int = 12,
        max_duration: float = 1,
        max_memory: int = 32 * 1024 * 1024,
        max_output: int = 16_384,
        timeout: float = 15,
    ) -> None:
        self.device = device
        self.max_calls = max_calls
        self.max_output = max_output
        self.timeout = timeout
        self.limits = {
            "max_feed_duration_secs": max_duration,
            "max_turn_duration_secs": max_duration,
            "max_memory": max_memory,
            "max_recursion_depth": 100,
            "max_suspensions": 256,
            "max_total_sleep_secs": 0,
        }
        self.pool_stack = AsyncExitStack()
        self.session_stack = AsyncExitStack()
        self.pool = None
        self.session = None
        self.bound: ScopedDevice | None = None
        self.wrapper: ClassInstance | None = None
        self.lock = asyncio.Lock()
        self.closed = False

    async def _reset(self) -> None:
        if self.bound:
            await self.bound.revoke()
        await self.session_stack.aclose()
        self.session = self.bound = self.wrapper = None

    async def execute(self, scope: str, code: str) -> dict:
        async with self.lock:
            if self.closed:
                raise RuntimeError("Device code executor has closed")
            calls: list[dict] = []
            output = CollectStreams(max_bytes=self.max_output)
            try:
                async with asyncio.timeout(self.timeout):
                    validate_code(code)
                    scope_status = await self.device.get_status(scope)
                    if self.bound is None or self.bound.scope != scope:
                        await self._reset()
                        if self.pool is None:
                            self.pool = await self.pool_stack.enter_async_context(
                                AsyncMonty(
                                    max_processes=1,
                                    checkout_timeout=3,
                                    request_timeout=3,
                                )
                            )
                        self.session = await self.session_stack.enter_async_context(
                            self.pool.checkout(
                                limits=self.limits,
                                type_check=True,
                                type_check_stubs=DEVICE_STUBS,
                            )
                        )
                        self.bound = ScopedDevice(self.device, scope, self.max_calls)
                        self.wrapper = ClassInstance(
                            self.bound,
                            allowed_methods={"set_orb", "show_text", "get_status"},
                        )
                    self.bound.calls = calls
                    self.bound.active = True
                    value = await self.session.feed_run(
                        code,
                        inputs={"device": self.wrapper},
                        print_callback=output,
                    )
                    return completed(
                        value,
                        "".join(text for _, text in output.output),
                        calls,
                        scope_status,
                        self.max_output,
                    )
            except asyncio.CancelledError:
                await self._reset()
                raise
            except Exception as error:
                await self._reset()
                return failed(
                    type(error).__name__,
                    str(error),
                    "".join(text for _, text in output.output),
                    calls,
                    self.max_output,
                )
            finally:
                if self.bound:
                    self.bound.active = False

    async def close(self) -> None:
        async with self.lock:
            self.closed = True
            await self._reset()
            await self.pool_stack.aclose()


class RunnerFailure(Exception):
    def __init__(self, error: dict, output: str, calls: list) -> None:
        super().__init__(error["message"])
        self.kind = error["type"]
        self.output = output
        self.calls = calls


class Runner:
    def __init__(self, write, drain, chunks: AsyncIterator, close, limit: int) -> None:
        self.write = write
        self.drain = drain
        self.chunks = chunks
        self._close = close
        self.limit = limit
        self.buffer = ""

    async def send(self, message: dict) -> None:
        self.write(json.dumps(message, allow_nan=False) + "\n")
        await self.drain()

    async def receive(self) -> dict:
        while "\n" not in self.buffer:
            if len(self.buffer) > self.limit:
                raise ValueError("Sandbox message exceeds the output limit")
            try:
                chunk = await anext(self.chunks)
            except StopAsyncIteration:
                raise RuntimeError("Sandbox runner exited") from None
            self.buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
        line, self.buffer = self.buffer.split("\n", 1)
        return json.loads(line)

    async def close(self) -> None:
        await self._close()


class ModalSandbox:
    def __init__(self, app_name: str = "slate-code", limit: int = 4 * 16_384) -> None:
        self.app_name = app_name
        self.limit = limit
        self.sandbox: modal.Sandbox | None = None
        self.spare: asyncio.Task[Runner] | None = None
        self.lock = asyncio.Lock()

    async def ensure(self) -> modal.Sandbox:
        async with self.lock:
            if self.sandbox is None:
                app = await modal.App.lookup.aio(self.app_name, create_if_missing=True)
                self.sandbox = await modal.Sandbox.create.aio(
                    app=app,
                    image=SANDBOX_IMAGE,
                    cpu=1,
                    memory=512,
                    timeout=3600,
                    idle_timeout=600,
                )
                logger.info("Modal sandbox ready: %s", self.sandbox.object_id)
            return self.sandbox

    async def _start(self) -> Runner:
        sandbox = await self.ensure()
        process = await sandbox.exec.aio("python", "-u", RUNNER_PATH, bufsize=1)

        async def close() -> None:
            process.stdin.write_eof()
            await process.stdin.drain.aio()
            await process.wait.aio()

        return Runner(
            process.stdin.write,
            process.stdin.drain.aio,
            aiter(process.stdout),
            close,
            self.limit,
        )

    async def spawn(self) -> Runner:
        spare, self.spare = self.spare, None
        try:
            runner = await spare if spare is not None else await self._start()
        except Exception:
            logger.warning("Sandbox runner failed to start; recreating", exc_info=True)
            await self.terminate()
            runner = await self._start()
        self.spare = asyncio.create_task(self._start())
        return runner

    async def terminate(self) -> None:
        async with self.lock:
            sandbox, self.sandbox = self.sandbox, None
            spare, self.spare = self.spare, None
        if spare is not None:
            spare.cancel()
            await asyncio.gather(spare, return_exceptions=True)
        if sandbox is not None:
            logger.warning("Terminating Modal sandbox %s", sandbox.object_id)
            await sandbox.terminate.aio()


class SandboxExecutor:
    """Runs agent code in a Modal sandbox. The sandbox calls the public device
    routes itself, authorized by the turn scope, so device calls never pass back
    through this process."""

    def __init__(
        self,
        device: DeviceClient,
        sandbox,
        url: str,
        *,
        max_calls: int = 12,
        max_output: int = 16_384,
        timeout: float = 15,
    ) -> None:
        self.device = device
        self.sandbox = sandbox
        self.url = url
        self.max_calls = max_calls
        self.max_output = max_output
        self.timeout = timeout
        self.runner: Runner | None = None
        self.scope: str | None = None
        self.closing: set[asyncio.Task] = set()
        self.lock = asyncio.Lock()
        self.closed = False

    async def _close_runner(self, runner: Runner) -> None:
        try:
            async with asyncio.timeout(5):
                await runner.close()
        except Exception:
            logger.exception("Idle sandbox runner did not exit after end of input")

    def _retire(self) -> None:
        runner, self.runner, self.scope = self.runner, None, None
        if runner is not None:
            task = asyncio.create_task(self._close_runner(runner))
            self.closing.add(task)
            task.add_done_callback(self.closing.discard)

    async def _discard(self) -> None:
        self.runner = self.scope = None
        for task in tuple(self.closing):
            task.cancel()
        await asyncio.gather(*self.closing, return_exceptions=True)
        await self.sandbox.terminate()

    async def execute(self, scope: str, code: str) -> dict:
        async with self.lock:
            if self.closed:
                raise RuntimeError("Device code executor has closed")
            in_flight = False
            try:
                async with asyncio.timeout(self.timeout):
                    validate_code(code)
                    scope_status = await self.device.get_status(scope)
                    if self.runner is None or self.scope != scope:
                        self._retire()
                        self.runner = await self.sandbox.spawn()
                        self.scope = scope
                    in_flight = True
                    await self.runner.send(
                        {
                            "code": code,
                            "url": self.url,
                            "scope": scope,
                            "max_calls": self.max_calls,
                        }
                    )
                    message = await self.runner.receive()
                    in_flight = False
                    if message["type"] == "failed":
                        raise RunnerFailure(
                            message["error"], message["output"], message["calls"]
                        )
                    return completed(
                        message["value"],
                        message["output"],
                        message["calls"],
                        scope_status,
                        self.max_output,
                    )
            except asyncio.CancelledError:
                await self._discard()
                raise
            except RunnerFailure as error:
                self._retire()
                return failed(
                    error.kind, str(error), error.output, error.calls, self.max_output
                )
            except Exception as error:
                if in_flight:
                    await self._discard()
                else:
                    self._retire()
                return failed(type(error).__name__, str(error), "", [], self.max_output)

    async def close(self) -> None:
        async with self.lock:
            self.closed = True
            await self._discard()
