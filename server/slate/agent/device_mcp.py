import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from slate.agent.code_mode import (
    DEVICE_STUBS,
    DeviceClient,
    ModalSandbox,
    MontyExecutor,
    PythonExecutor,
    SandboxExecutor,
)
from slate.device import DeviceStatus

CREDENTIAL_SECONDS = 135


def build_server(*, mode: str | None = None) -> MCPServer:
    selected = mode or os.getenv("SLATE_DEVICE_MODE", "monty")
    if selected not in ("tools", "monty", "modal"):
        raise ValueError(f"Unknown SLATE_DEVICE_MODE: {selected}")
    client = httpx.AsyncClient(
        base_url=os.getenv("SLATE_DEVICE_URL", "http://127.0.0.1:8000"),
        timeout=3,
    )
    device = DeviceClient(client)
    sandbox = ModalSandbox()
    executor = (
        SandboxExecutor(device, sandbox, os.environ["SLATE_PUBLIC_URL"])
        if selected == "modal"
        else MontyExecutor(device)
    )

    @asynccontextmanager
    async def lifespan(server):
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(client.aclose)
            cleanup.push_async_callback(executor.close)
            if selected == "modal":
                await sandbox.ensure()
            yield

    server = MCPServer("Slate device", lifespan=lifespan)
    python = PythonExecutor()

    @server.tool(
        name="run_python",
        description=(
            "Run a short Python calculation or data transformation in Monty. "
            "No device, files, network, packages, or secrets. Each call starts fresh. "
            "Print output or return a final expression. Limited to 1 second of Python, "
            "32 MiB of memory, and 16384 bytes each of code, output, and result."
        ),
        structured_output=True,
    )
    async def run_python(code: str) -> dict[str, Any]:
        return await python.execute(code)

    async def credentials(method: str, path: str, scope: str, **kwargs) -> Any:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scope):
            raise ValueError("Scope must be an opaque capability identifier")
        response = await client.request(
            method,
            f"/api/agent/credentials{path}",
            headers={"Authorization": f"Bearer {scope}"},
            **kwargs,
        )
        if response.is_error:
            detail = response.json().get("detail", "Credential request failed")
            raise RuntimeError(f"Credential HTTP {response.status_code}: {detail}")
        return response.json()

    @server.tool(
        name="list_credentials",
        description=(
            "List the user's saved logins, cards and tokens for the active scope: "
            "id, service, label and kind. Never returns secret values."
        ),
    )
    async def list_credentials(scope: str) -> list[dict[str, Any]]:
        return await credentials("GET", "", scope, timeout=5)

    @server.tool(
        name="use_credential",
        description=(
            "Get one saved credential's values for the active scope's task. "
            "If the user has not allowed it for this task, Slate asks them first "
            "and waits up to two minutes. Give a short purpose they will "
            "recognize, like 'pay the October electric bill'. Type the values "
            "only where they are needed; never say, show or store them."
        ),
        structured_output=True,
    )
    async def use_credential(
        scope: str, credential_id: str, purpose: str
    ) -> dict[str, Any]:
        return await credentials(
            "POST",
            f"/{credential_id}/use",
            scope,
            json={"purpose": purpose},
            timeout=CREDENTIAL_SECONDS,
        )

    if selected == "tools":

        @server.tool(
            name="device_set_orb",
            description=(
                "Set #RRGGBB orb, radius 10..45; return firmware acknowledgment."
            ),
            structured_output=True,
        )
        async def set_orb(scope: str, color: str, radius: float = 24) -> DeviceStatus:
            return DeviceStatus.model_validate(
                await device.set_orb(scope, color, radius)
            )

        @server.tool(
            name="device_show_text",
            description=(
                "Show up to 64 printable ASCII characters; return firmware receipt."
            ),
            structured_output=True,
        )
        async def show_text(scope: str, text: str) -> DeviceStatus:
            return DeviceStatus.model_validate(await device.show_text(scope, text))

        @server.tool(
            name="device_get_status",
            description="Read acknowledged firmware state for this active turn.",
            structured_output=True,
        )
        async def get_status(scope: str) -> DeviceStatus:
            return DeviceStatus.model_validate(await device.get_status(scope))
    else:
        description = (
            "Compose the device SDK in one sandboxed program for the active scope. "
            "Pass the opaque scope from the active turn instructions as scope. "
            "Await device.set_orb(color, radius), device.show_text(text), "
            "and device.get_status(). Color is #RRGGBB; radius is 10..45; "
            "text is printable ASCII with at most 64 characters. "
            "Results include actual firmware receipts. "
            + "Python REPL state persists within this scope; failures reset it. "
            "The final expression returns a value. "
            + (
                "Runs in Monty, a restricted Python subset with no file, network "
                "or shell access. "
                if selected == "monty"
                else "Runs in CPython 3.12 with the standard library, httpx and "
                "internet access inside an isolated Modal container. "
            )
            + "Never blindly replay failed code: completed actions remain applied.\n"
            + DEVICE_STUBS
        )

        @server.tool(
            name="execute_device_code", description=description, structured_output=True
        )
        async def execute_device_code(scope: str, code: str) -> dict[str, Any]:
            return await executor.execute(scope, code)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
