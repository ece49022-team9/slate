import asyncio
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI
from slate.agent.code_mode import (
    DeviceClient,
    MontyExecutor,
)
from slate.breadboard import breadboard, build_image
from slate.device import DeviceSDK, FirmwareDevice, router

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def device_http(voice):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.voice = voice
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", lifespan="off")
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("Device fixture HTTP server did not start")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


async def verify(bench, voice, device_url):
    turn = SimpleNamespace(scope=uuid4().hex, id=uuid4().hex)
    commands = []
    peer = FirmwareDevice(bench.link, lambda: turn.id)

    async def execute(command):
        commands.append(command)
        return (await peer.execute(command)).model_dump()

    sdk = DeviceSDK(turn.scope, turn.id, execute, lambda: voice.current is session)
    session = SimpleNamespace(turn=turn, device_sdk=lambda current_turn: sdk)
    voice.current = session
    async with AsyncExitStack() as resources:
        client = await resources.enter_async_context(
            httpx.AsyncClient(base_url=device_url)
        )
        device = DeviceClient(client)
        executor = MontyExecutor(device)
        resources.push_async_callback(executor.close)
        initial = await device.get_status(turn.scope)
        small = await executor.execute(
            turn.scope,
            "await device.set_orb('#0000ff', 12)",
        )
        assert small["status"] == "completed", small
        small_receipt = small["result"]
        await bench.sleep(0.1)
        small_pixels = bench.oled.frames[-1].pixels
        assert small_pixels[63 * 128 + 63] == 0x001F, "Small orb did not turn blue"
        composed = await executor.execute(
            turn.scope,
            "orb = await device.set_orb('#0000ff', 40)\n"
            "text = await device.show_text('A')\n"
            "status = await device.get_status()\n[orb, text, status]",
        )
        assert composed["status"] == "completed", composed
        large, text, status = composed["result"]
        assert [call["receipt"] for call in composed["calls"]] == composed["result"]
        assert initial["revision"] < small_receipt["revision"] < large["revision"]
        assert large["revision"] < text["revision"] == status["revision"]
        assert status["color"] == "#0000ff" and status["text"] == "A"
        assert status["radius"] == 40
        await bench.sleep(0.1)
        pixels = bench.oled.frames[-1].pixels
        assert sum(bool(pixel) for pixel in pixels[: 90 * 128]) > 2 * sum(
            bool(pixel) for pixel in small_pixels[: 90 * 128]
        ), "SPI pixels did not reflect the larger orb"
        for column, bits in enumerate((0x7C, 0x12, 0x11, 0x12, 0x7C)):
            for row in range(8):
                expected = 0xFFFF if bits & (1 << row) else 0
                assert pixels[(96 + row) * 128 + 1 + column] == expected, (
                    "OLED SPI pixels do not match the requested A glyph"
                )
        partial = await executor.execute(
            turn.scope,
            "await device.set_orb('#00ff00', 20)\nawait device.show_text('\\n')",
        )
        assert partial["status"] == "error", partial
        acknowledged = partial["calls"][0]
        assert acknowledged["status"] == "completed", partial
        assert acknowledged["receipt"]["color"] == "#00ff00"
        after = await device.get_status(turn.scope)
        assert after["revision"] == status["revision"] + 1
        assert after["revision"] == acknowledged["receipt"]["revision"]
        assert (after["color"], after["radius"], after["text"]) == ("#00ff00", 20, "A")
        await bench.sleep(0.1)
        assert bench.oled.frames[-1].pixels[63 * 128 + 63] == 0x07E0
        assert sum(command.operation == "set_orb" for command in commands) == 3
        assert sum(command.operation == "show_text" for command in commands) == 1
        assert len({command.request_id for command in commands}) == len(commands)
        count = len(commands)
        voice.current = None
        try:
            expired = await executor.execute(turn.scope, "123")
        except RuntimeError as error:
            assert "ended" in str(error), error
        else:
            assert expired["status"] == "error", expired
            assert "ended" in expired["error"]["message"], expired
        assert len(commands) == count, "Expired scope reached the firmware"
        print(
            "slate.device_code: monty passed real HTTP + QEMU firmware receipts; "
            "color, radius, glyph SPI pixels, partial failure and scope expiry verified"
        )


async def run():
    voice = SimpleNamespace(current=None)
    async with breadboard() as bench, device_http(voice) as device_url:
        await verify(bench, voice, device_url)
    print(
        "slate.device_code: the device socket was replaced by the fixture; "
        "HTTP routes, typed SDK, Monty and QEMU firmware were real"
    )


def main():
    build_image()
    asyncio.run(run())


if __name__ == "__main__":
    main()
