import argparse
import asyncio
import json
import logging
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from slate.browser.modal import live

logger = logging.getLogger("slate.browser.view")
HERE = Path(__file__).parent
PORT = 9224
POLL_SECONDS = 0.5


@dataclass
class Machine:
    base: str
    token: str | None

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def socket(self, path: str) -> str:
        url = self.base.replace("https://", "wss://").replace("http://", "ws://") + path
        joiner = "&" if "?" in path else "?"
        return url + f"{joiner}_modal_connect_token={self.token}" if self.token else url


async def find(url: str | None) -> Machine:
    if url:
        return Machine(url.rstrip("/"), None)
    parts = urlsplit((await live())["cdp_url"])
    return Machine(
        f"https://{parts.netloc}", parse_qs(parts.query)["_modal_connect_token"][0]
    )


class Viewer:
    def __init__(self, url: str | None) -> None:
        self.url = url
        self.machine: Machine | None = None
        self.viewers: set[WebSocket] = set()
        self.state = {
            "page": None,
            "actions": [],
            "errors": [],
            "held": False,
            "online": False,
        }
        self.version: int | None = None
        self.stills: dict[int, bytes] = {}

    async def follow(self) -> None:
        while True:
            try:
                self.machine = await find(self.url)
                async with httpx.AsyncClient(
                    headers=self.machine.headers(), timeout=10
                ) as client:
                    while True:
                        await self.poll(client)
                        await asyncio.sleep(POLL_SECONDS)
            except Exception:
                logger.exception("slate.browser.view: lost the browser machine")
            self.machine = None
            self.version = None
            await self.publish({"page": None, "online": False})
            await asyncio.sleep(3)

    async def poll(self, client: httpx.AsyncClient) -> None:
        params = {"version": self.version} if self.version is not None else {}
        response = await client.get(self.machine.base + "/live", params=params)
        response.raise_for_status()
        state = response.json()
        if state["version"] == self.version and self.state["online"]:
            return
        self.version = state["version"]
        update = {"online": True}
        if "actions" in state:
            update |= {key: state[key] for key in ("page", "actions", "errors", "held")}
        await self.publish(update)

    async def publish(self, update: dict) -> None:
        self.state |= update
        text = json.dumps(self.state)
        for viewer in list(self.viewers):
            try:
                await viewer.send_text(text)
            except Exception:
                self.viewers.discard(viewer)

    async def still(self, number: int) -> bytes | None:
        if number in self.stills:
            return self.stills[number]
        if not self.machine:
            return None
        async with httpx.AsyncClient(
            headers=self.machine.headers(), timeout=10
        ) as client:
            response = await client.get(f"{self.machine.base}/stills/{number}")
        if response.status_code != 200:
            logger.warning(
                "slate.browser.view: still %s answered %s", number, response.status_code
            )
            return None
        self.stills[number] = response.content
        return response.content

    async def screen(self, socket: WebSocket) -> None:
        if not self.machine:
            await socket.close(code=1013)
            return
        async with websockets.connect(
            self.machine.socket("/screen"), max_size=None
        ) as upstream:

            async def down() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await socket.send_bytes(message)
                    else:
                        await socket.send_text(message)

            async def up() -> None:
                while True:
                    message = await socket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await upstream.send(
                        message.get("bytes") or message.get("text") or ""
                    )

            pumps = [asyncio.create_task(down()), asyncio.create_task(up())]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for pump in pumps:
                pump.cancel()

    async def hold(self, socket: WebSocket) -> None:
        if not self.machine:
            await socket.close(code=1013)
            return
        mode = quote(socket.query_params.get("mode", "auto"))
        async with websockets.connect(
            self.machine.socket(f"/hold?mode={mode}")
        ) as upstream:
            await upstream.recv()
            await socket.send_text("held")
            logger.info("slate.browser.view: you have the browser")
            waits = [
                asyncio.create_task(upstream.wait_closed()),
                asyncio.create_task(until_closed(socket)),
            ]
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for wait in waits:
                wait.cancel()
        logger.info("slate.browser.view: handed the browser back")


async def until_closed(socket: WebSocket) -> None:
    while (await socket.receive())["type"] != "websocket.disconnect":
        pass


def create(viewer: Viewer) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse((HERE / "view.html").read_text())

    @app.get("/stills/{number}")
    async def still(number: int) -> Response:
        picture = await viewer.still(number)
        if picture is None:
            return Response(status_code=404)
        return Response(picture, media_type="image/jpeg")

    @app.websocket("/state")
    async def state(socket: WebSocket) -> None:
        await socket.accept()
        viewer.viewers.add(socket)
        await socket.send_text(json.dumps(viewer.state))
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            viewer.viewers.discard(socket)

    @app.websocket("/screen")
    async def screen(socket: WebSocket) -> None:
        await socket.accept()
        try:
            await viewer.screen(socket)
        except (OSError, websockets.ConnectionClosed) as error:
            logger.warning("slate.browser.view: the screen stream ended: %r", error)

    @app.websocket("/hold")
    async def hold(socket: WebSocket) -> None:
        await socket.accept()
        try:
            await viewer.hold(socket)
        except (OSError, websockets.ConnectionClosed) as error:
            logger.warning("slate.browser.view: the hold ended: %r", error)
        with suppress(RuntimeError, WebSocketDisconnect):
            await socket.close()

    return app


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url", help="a browser machine's proxy, such as http://127.0.0.1:9222"
    )
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    viewer = Viewer(args.url)
    config = uvicorn.Config(
        create(viewer), host="127.0.0.1", port=args.port, log_level="warning"
    )
    url = f"http://127.0.0.1:{args.port}"
    print(f"slate.browser.view: watching the agent's browser at {url}")
    webbrowser.open(url)
    await asyncio.gather(uvicorn.Server(config).serve(), viewer.follow())


if __name__ == "__main__":
    asyncio.run(main())
