import struct
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

from slate.simulator import ROOT, qemu


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with qemu() as firmware:
        app.state.firmware = firmware
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(Path(__file__).with_suffix(".html"))


@app.get("/frame")
async def frame(request: Request):
    data = await request.app.state.firmware.exchange(5)
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/state/{state}")
async def state(state: int, request: Request):
    if not 0 <= state <= 5:
        raise HTTPException(400, "Unknown display state")
    await request.app.state.firmware.exchange(6, struct.pack("B", state))
    return {"state": state}


def main():
    subprocess.run(["bash", "scripts/esp-idf.sh", "build"], cwd=ROOT, check=True)
    uvicorn.run(app, host="127.0.0.1", port=8010)


if __name__ == "__main__":
    main()
