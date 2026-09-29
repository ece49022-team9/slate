import struct
from array import array
from contextlib import asynccontextmanager
from pathlib import Path

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
    request.app.state.bench.feed(stereo(tone(440, 8000, 1.5, rate), request.app))
    return {"tone": 440}


def stereo(samples, app: FastAPI) -> bytes:
    mono = array("h", samples)
    both = array("h", bytes(len(mono) * 4))
    both[app.state.bench.slot :: 2] = mono
    return both.tobytes()


@app.websocket("/mic")
async def microphone(socket: WebSocket):
    await socket.accept()
    try:
        while True:
            data = await socket.receive_bytes()
            socket.app.state.bench.feed(stereo(array("h", data), socket.app))
    except WebSocketDisconnect:
        return


def main():
    build_image()
    uvicorn.run(app, host="127.0.0.1", port=8010)


if __name__ == "__main__":
    main()
