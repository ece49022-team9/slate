import os
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from slate.agent.code_mode import (
    DEVICE_STUBS,
    DEVICE_TYPESCRIPT,
    CloudflareExecutor,
    DeviceClient,
    MontyExecutor,
)
from slate.device import DeviceStatus


def build_server(*, mode: str | None = None) -> MCPServer:
    selected = mode or os.getenv("SLATE_DEVICE_MODE", "monty")
    if selected not in ("tools", "monty", "cloudflare"):
        raise ValueError(f"Unknown SLATE_DEVICE_MODE: {selected}")
    client = httpx.AsyncClient(
        base_url=os.getenv("SLATE_DEVICE_URL", "http://127.0.0.1:8000"),
        timeout=3,
    )
    device = DeviceClient(client)
    monty = MontyExecutor(device)
    cloudflare_client = httpx.AsyncClient(
        base_url=os.getenv("SLATE_CLOUDFLARE_CODE_URL", "http://127.0.0.1:8650"),
        timeout=15,
    )
    cloudflare = CloudflareExecutor(device, cloudflare_client)

    @asynccontextmanager
    async def lifespan(server):
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(cloudflare_client.aclose)
            cleanup.push_async_callback(client.aclose)
            cleanup.push_async_callback(monty.close)
            yield

    server = MCPServer("Slate device", lifespan=lifespan)
    if selected == "tools":

        @server.tool(name="device_set_orb", structured_output=True)
        async def set_orb(scope: str, color: str, radius: float = 24) -> DeviceStatus:
            """Set #RRGGBB orb, radius 10..45; return firmware acknowledgment."""
            return DeviceStatus.model_validate(
                await device.set_orb(scope, color, radius)
            )

        @server.tool(name="device_show_text", structured_output=True)
        async def show_text(scope: str, text: str) -> DeviceStatus:
            """Show up to 64 printable ASCII characters; return firmware receipt."""
            return DeviceStatus.model_validate(await device.show_text(scope, text))

        @server.tool(name="device_get_status", structured_output=True)
        async def get_status(scope: str) -> DeviceStatus:
            """Read acknowledged firmware state for this active turn."""
            return DeviceStatus.model_validate(await device.get_status(scope))
    else:
        signatures = DEVICE_STUBS if selected == "monty" else DEVICE_TYPESCRIPT
        description = (
            "Compose the device SDK in one sandboxed program for the active scope. "
            "Pass the opaque scope from the active turn instructions as scope. "
            "Await device.set_orb(color, radius), device.show_text(text), "
            "and device.get_status(). Color is #RRGGBB; radius is 10..45; "
            "text is printable ASCII with at most 64 characters. "
            "Results include actual firmware receipts. No file, network, shell, "
            "or other host capabilities. "
            + (
                "Python REPL state persists within this scope; failures reset it. "
                "The final expression returns a value. "
                if selected == "monty"
                else "JavaScript runs in a separate Cloudflare Workers/V8 runtime. "
                "Supply an async arrow function and return its result. "
            )
            + "Never blindly replay failed code: completed actions remain applied.\n"
            + signatures
        )

        @server.tool(
            name="execute_device_code", description=description, structured_output=True
        )
        async def execute_device_code(scope: str, code: str) -> dict[str, Any]:
            if selected == "monty":
                return await monty.execute(scope, code)
            return await cloudflare.execute(scope, code)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
