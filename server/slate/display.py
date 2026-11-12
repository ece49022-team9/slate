import asyncio
import struct
from array import array
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from slate.board import check, load
from slate.breadboard import breadboard, build_image, tone


class Keys(BaseModel):
    text: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with breadboard(realtime=True) as bench:
        app.state.bench = bench
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(Path(__file__).with_suffix(".html"))


@app.get("/board")
async def board():
    board, parts = load()
    mcu = parts["mcu"][board["mcu"]]
    return {
        "mcu": {"id": board["mcu"], "name": mcu["name"]},
        "supply_v": board["supply_v"],
        "devices": [
            {
                "name": name,
                "part": parts["part"][device["part"]]["name"],
                "pins": device["pins"],
                "settings": {
                    key: value
                    for key, value in device.items()
                    if key not in ("part", "pins")
                },
            }
            for name, device in board["device"].items()
        ],
        "problems": [
            {"level": p.level, "device": p.device, "message": p.message}
            for p in check(board, parts)
        ],
    }


@app.get("/status")
async def status(request: Request):
    bench = request.app.state.bench
    frames = list(bench.oled.frames)[-20:]
    fps = (
        (len(frames) - 1) / ((frames[-1].ns - frames[0].ns) / 1e9)
        if len(frames) > 1
        else 0
    )
    return {
        "now": bench.now,
        "state": bench.link.state(),
        "fps": round(fps, 1),
        "levels": bench.levels,
        "toggles": bench.toggles,
        "spi_bytes": bench.spi_bytes,
        "audio_bytes": bench.audio_bytes,
        "lines": bench.link.lines[-60:],
    }


@app.get("/frame")
async def frame(request: Request):
    bench = request.app.state.bench
    if not bench.oled.frames:
        raise HTTPException(503, "The panel has not drawn a frame yet")
    pixels = bench.oled.frames[-1].pixels
    header = struct.pack("<HHII", 128, 128, bench.link.state(), bench.oled.count)
    return Response(
        header + struct.pack(f"<{len(pixels)}H", *pixels),
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/state/{state}")
async def state(state: int, request: Request):
    if not 0 <= state <= 5:
        raise HTTPException(400, "Unknown display state")
    request.app.state.bench.link.type(str(state))
    return {"state": state}


@app.post("/serial")
async def serial(keys: Keys, request: Request):
    request.app.state.bench.link.type(keys.text)
    return {"sent": keys.text}


@app.post("/tone")
async def play_tone(request: Request):
    board, _ = load()
    rate = board["device"]["mic"]["sample_hz"]
    bench = request.app.state.bench
    bench.feed(mic_slot_pcm(tone(440, 8000, 1.5, rate), bench.slot))
    return {"tone": 440}


def mic_slot_pcm(samples: Iterable[int], slot: int) -> bytes:
    mono = array("h", samples)
    both = array("h", [0]) * (len(mono) * 2)
    both[slot::2] = mono
    return both.tobytes()


@app.websocket("/mic")
async def microphone(socket: WebSocket):
    await socket.accept()
    try:
        while True:
            data = await socket.receive_bytes()
            bench = socket.app.state.bench
            bench.feed(mic_slot_pcm(array("h", data), bench.slot))
    except WebSocketDisconnect:
        return


@app.post("/speaker-test")
async def speaker_test(request: Request):
    bench = request.app.state.bench
    if getattr(bench, "speaker_testing", False):
        raise HTTPException(409, "A speaker test is already running")
    bench.speaker_testing = True
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)
    bench.speaker_listeners.add(queue)
    pcm = bytearray()
    started = False
    try:
        bench.link.type("t")
        # Capture the finite firmware test before playing it. QEMU may advance
        # more slowly than wall time; a live 100 ms buffer cannot cover that.
        async with asyncio.timeout(45):
            while len(pcm) < 80_000:
                packet = await queue.get()
                if not started:
                    first = next(
                        (i for i in range(0, len(packet), 4) if any(packet[i : i + 4])),
                        None,
                    )
                    if first is None:
                        continue
                    # Preserve the zero sample immediately before the sine starts.
                    packet = packet[max(0, first - 4) :]
                    started = True
                pcm.extend(packet)
        return Response(
            bytes(pcm[:80_000]),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )
    except TimeoutError:
        raise HTTPException(
            504, "Timed out waiting for firmware speaker audio"
        ) from None
    finally:
        bench.speaker_listeners.discard(queue)
        bench.speaker_testing = False


@app.get("/speaker-worklet.js")
async def speaker_worklet():
    return FileResponse(
        Path(__file__).with_name("speaker-worklet.js"),
        media_type="text/javascript",
    )


@app.websocket("/speaker")
async def speaker(socket: WebSocket):
    await socket.accept()
    bench = socket.app.state.bench
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
    bench.speaker_listeners.add(queue)

    async def send():
        while True:
            payload = await queue.get()
            async with asyncio.timeout(1):
                await socket.send_bytes(payload)

    async def receive():
        while True:
            await socket.receive_bytes()

    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, ConnectionError, TimeoutError):
        pass
    finally:
        bench.speaker_listeners.discard(queue)
        for task in tasks:
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await socket.close()
            except RuntimeError:
                pass


def main():
    build_image()
    uvicorn.run(app, host="127.0.0.1", port=8010)


if __name__ == "__main__":
    main()
