import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack

import httpx
from pydantic_monty import AsyncMonty, ClassInstance, CollectStreams

from slate.device import DeviceStatus, OrbRequest, TextRequest

logger = logging.getLogger("slate.agent.code_mode")
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
DEVICE_TYPESCRIPT = """interface DeviceReceipt {
  request_id: string; operation: string; revision: number; state: number;
  color: string; radius: number; text: string; custom: boolean;
}
declare const device: {
  set_orb(color: string, radius?: number): Promise<DeviceReceipt>;
  show_text(text: string): Promise<DeviceReceipt>;
  get_status(): Promise<DeviceReceipt>;
};
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
            f"/api/device/{scope}/{operation}",
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
                    if not code.strip() or len(code.encode()) > 16_384:
                        raise ValueError("Code must contain 1..16384 UTF-8 bytes")
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
                    printed = "".join(text for _, text in output.output)
                    if (
                        len(json.dumps(value, allow_nan=False).encode())
                        > self.max_output
                    ):
                        raise ValueError("Code result exceeds the output limit")
                    return {
                        "status": "completed",
                        "result": value,
                        "output": printed,
                        "calls": calls,
                        "scope_status": scope_status,
                        "state_reset": False,
                    }
            except asyncio.CancelledError:
                await self._reset()
                raise
            except Exception as error:
                logger.warning("Device code failed: error=%s", type(error).__name__)
                await self._reset()
                return {
                    "status": "error",
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error)
                        .encode()[: self.max_output]
                        .decode(errors="ignore"),
                    },
                    "output": "".join(text for _, text in output.output),
                    "calls": calls,
                    "state_reset": True,
                }
            finally:
                if self.bound:
                    self.bound.active = False

    async def close(self) -> None:
        async with self.lock:
            self.closed = True
            await self._reset()
            await self.pool_stack.aclose()


class CloudflareExecutor:
    def __init__(self, device: DeviceClient, client: httpx.AsyncClient) -> None:
        self.device = device
        self.client = client

    async def execute(self, scope: str, code: str) -> dict:
        await self.device.get_status(scope)
        if not code.strip() or len(code.encode()) > 16_384:
            raise ValueError("Code must contain 1..16384 UTF-8 bytes")
        async with asyncio.timeout(15):
            response = await self.client.post(
                "/execute",
                json={
                    "scope": scope,
                    "code": code,
                    "device_url": str(self.device.client.base_url),
                },
                timeout=15,
            )
            response.raise_for_status()
            if len(response.content) > 65_536:
                raise ValueError("Cloudflare execution output exceeds the output limit")
            return response.json()
