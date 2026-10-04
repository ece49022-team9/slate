import asyncio
import struct
from array import array
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from slate.board import check, load
from slate.breadboard import breadboard, build_image, tone
from slate.voice.sim import SimCall


class Keys(BaseModel):
    text: str


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    live: StrictBool = True
    echo: float = Field(0.0, ge=0, le=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with breadboard(realtime=True) as bench:
        app.state.bench = bench
        app.state.call = None
        try:
            yield
        finally:
            if app.state.call:
                await app.state.call.stop()


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


def active_call(request: Request) -> SimCall:
    call = request.app.state.call
    if call is None:
        raise HTTPException(409, "Start a call first")
    return call


@app.post("/call")
async def start_call(body: CallRequest, request: Request):
    if request.app.state.call is not None:
        raise HTTPException(409, "A call is already running")
    call = SimCall(request.app.state.bench, live=body.live, echo=body.echo)
    await call.start()
    request.app.state.call = call
    return await call.status()


@app.get("/call")
async def call_status(request: Request):
    call = request.app.state.call
    if call is None:
        return {"active": False}
    return await call.status()


@app.delete("/call")
async def end_call(request: Request):
    call = active_call(request)
    request.app.state.call = None
    return await call.stop()


@app.post("/call/talk")
async def press(request: Request):
    try:
        active_call(request).talk()
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"talking": True}


@app.delete("/call/talk")
async def release(request: Request):
    active_call(request).release()
    return {"talking": False}


@app.websocket("/speaker")
async def speaker(socket: WebSocket):
    await socket.accept()
    call = socket.app.state.call
    if call is None:
        await socket.close(1008, "Start a call first")
        return
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
    call.speakers.add(queue)
    try:
        while pcm := await queue.get():
            await socket.send_bytes(pcm)
        await socket.close()
    except WebSocketDisconnect:
        return
    finally:
        call.speakers.discard(queue)


def main():
    build_image()
    uvicorn.run(app, host="127.0.0.1", port=8010)


if __name__ == "__main__":
    main()
