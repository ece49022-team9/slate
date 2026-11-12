import ast
import asyncio
import contextlib
import io
import json
import sys

import httpx

RESULT = "__slate_result__"
OPERATIONS = {"set_orb": "POST", "show_text": "POST", "status": "GET"}


class DeviceError(Exception):
    pass


class Device:
    def __init__(self, client: httpx.AsyncClient, max_calls: int) -> None:
        self._client = client
        self._max_calls = max_calls
        self.calls: list[dict] = []

    async def _request(self, operation: str, body: dict | None = None) -> dict:
        name = "get_status" if operation == "status" else operation
        if len(self.calls) >= self._max_calls:
            raise DeviceError("Device tool-call budget exceeded")
        call = {"tool": name, "arguments": body or {}, "status": "started"}
        self.calls.append(call)
        try:
            response = await self._client.request(
                OPERATIONS[operation], f"/api/device/{operation}", json=body
            )
            if response.is_error:
                detail = response.json().get("detail", "Device request failed")
                raise DeviceError(f"Device HTTP {response.status_code}: {detail}")
            receipt = response.json()
            if receipt.get("operation") != name:
                raise DeviceError("Device acknowledgment does not match the request")
        except BaseException as error:
            call.update(status="failed", error_type=type(error).__name__)
            raise
        call.update(status="completed", receipt=receipt)
        return receipt

    async def set_orb(self, color: str, radius: float = 24) -> dict:
        return await self._request("set_orb", {"color": color, "radius": radius})

    async def show_text(self, text: str) -> dict:
        return await self._request("show_text", {"text": text})

    async def get_status(self) -> dict:
        return await self._request("status")


async def run(code: str, namespace: dict):
    tree = ast.parse(code, "<agent>", "exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tree.body[-1] = ast.Assign(
            targets=[ast.Name(RESULT, ast.Store())], value=tree.body[-1].value
        )
        ast.fix_missing_locations(tree)
    namespace.pop(RESULT, None)
    program = compile(tree, "<agent>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    pending = eval(program, namespace)
    if pending is not None:
        await pending
    return namespace.pop(RESULT, None)


def send(message: dict) -> None:
    sys.__stdout__.write(json.dumps(message, allow_nan=False) + "\n")
    sys.__stdout__.flush()


async def main() -> None:
    loop = asyncio.get_running_loop()
    async with contextlib.AsyncExitStack() as stack:
        namespace: dict = {"__name__": "__slate__"}
        device: Device | None = None
        while line := await loop.run_in_executor(None, sys.stdin.readline):
            message = json.loads(line)
            if device is None:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=message["url"],
                        headers={"Authorization": f"Bearer {message['scope']}"},
                        timeout=5,
                    )
                )
                device = namespace["device"] = Device(client, message["max_calls"])
            device.calls = []
            printed = io.StringIO()
            try:
                with contextlib.redirect_stdout(printed):
                    value = await run(message["code"], namespace)
                send(
                    {
                        "type": "done",
                        "value": value,
                        "output": printed.getvalue(),
                        "calls": device.calls,
                    }
                )
            except Exception as error:
                send(
                    {
                        "type": "failed",
                        "error": {"type": type(error).__name__, "message": str(error)},
                        "output": printed.getvalue(),
                        "calls": device.calls,
                    }
                )


if __name__ == "__main__":
    asyncio.run(main())
