import struct
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

from slate.board import load
from slate.breadboard import breadboard, build_image, tone


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with breadboard() as bench:
        app.state.bench = bench
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(Path(__file__).with_suffix(".html"))


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
    await request.app.state.bench.link.type(str(state))
    return {"state": state}


@app.post("/tone")
async def play_tone(request: Request):
    board, _ = load()
    rate = board["device"]["mic"]["sample_hz"]
    await request.app.state.bench.speak(tone(440, 8000, 1.5, rate))
    return {"tone": 440}


def main():
    build_image()
    uvicorn.run(app, host="127.0.0.1", port=8010)


if __name__ == "__main__":
    main()
