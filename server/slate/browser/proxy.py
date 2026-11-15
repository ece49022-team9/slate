import asyncio
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit

from aiohttp import ClientSession, WSMsgType, web

from slate.browser.actions import Notes, Recorder
from slate.browser.cdp import CdpError, Devtools, Outbox

logger = logging.getLogger("slate.browser.proxy")
CHROME = "127.0.0.1:9223"
SCREEN = "ws://127.0.0.1:6080/ws"
MAX_MESSAGE = 256 * 1024 * 1024
HEADING = ("Page.frameStartedNavigating", "Page.frameRequestedNavigation")
TAKEN = (
    "The user has taken over the browser. "
    "Wait until they hand it back, then read the page again."
)
TAKEN_PAUSED = (
    "The user has taken over the browser. "
    "Your next browser action waits until they hand it back."
)
HOLD_MODES = ("refuse", "resume", "brief", "look", "auto")
WAIT_SLICE_SECONDS = 20
WATCH_SECONDS = 25
HEARD_WHILE_HELD = ("Target.attachedToTarget", "Target.detachedFromTarget")
REFUSED_WHILE_HELD = ("Target.createTarget", "Browser.close", "Browser.crash")
SCREEN_INPUT = ("m", "b", "sp", "k", "r", "c")
SCREEN_TOUCH = ("b", "sp", "k", "c")
HOLD_HEARTBEAT_SECONDS = 10


@dataclass
class Page:
    target: str
    url: str
    wanted: bool = False
    on: bool = False


def on_machine(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname or ""
    local = (
        host in ("localhost", "::1")
        or host.endswith(".localhost")
        or host.startswith("127.")
    )
    return parts.scheme in ("http", "https") and local


class Hold:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.holders = 0
        self.connections: set[Connection] = set()
        self.mode = "refuse"
        self.since = 0.0
        self.before: dict | None = None
        self.released = asyncio.Event()
        self.flipped = asyncio.Event()
        self.touched = False
        self.last: dict = {}

    @property
    def held(self) -> bool:
        return self.holders > 0

    @property
    def taken(self) -> str:
        return TAKEN if self.mode == "refuse" else TAKEN_PAUSED

    @property
    def paused(self) -> bool:
        return self.held and self.mode != "refuse"

    def take(self, mode: str) -> None:
        self.holders += 1
        if self.holders == 1:
            logger.info("slate.browser.proxy: user took the browser (%s)", mode)
            self.mode = mode
            self.since = time.time()
            self.before = self.recorder.page
            self.released = asyncio.Event()
            self.touched = False
            self.recorder.holding(True)
            self.flip()

    def release(self) -> None:
        self.holders -= 1
        if self.holders > 0:
            return
        logger.info("slate.browser.proxy: user handed it back")
        self.last = {
            "then": self.mode,
            "seconds": round(time.time() - self.since, 1),
            "before": self.before,
            "after": self.recorder.page,
            "visited": self.recorder.trail,
            "touched": self.touched,
        }
        self.recorder.holding(False)
        self.released.set()
        self.flip()
        for connection in list(self.connections):
            connection.back_again()

    def flip(self) -> None:
        self.flipped.set()
        self.flipped = asyncio.Event()

    async def watch(self, paused: bool) -> dict:
        if self.paused == paused:
            flipped = self.flipped
            with suppress(TimeoutError):
                await asyncio.wait_for(flipped.wait(), WATCH_SECONDS)
        return {"paused": self.paused}

    async def wait(self) -> dict:
        if not self.paused:
            return {"then": "go"}
        released = self.released
        try:
            await asyncio.wait_for(released.wait(), WAIT_SLICE_SECONDS)
        except TimeoutError:
            return {"then": "held"}
        return self.last


class Connection:
    def __init__(
        self, client: web.WebSocketResponse, upstream, recorder: Recorder, hold: Hold
    ) -> None:
        self.client = client
        self.upstream = upstream
        self.hold = hold
        self.pages: dict[str, Page] = {}
        self.told: dict[str, dict] = {}
        self.chrome = Outbox(upstream.send_str)
        self.agent = Outbox(client.send_str)
        self.devtools = Devtools(self.chrome.put)
        self.notes = Notes(self.devtools, recorder, self.pages)
        self.tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        workers = [
            asyncio.create_task(self.chrome.run()),
            asyncio.create_task(self.agent.run()),
            asyncio.create_task(self.from_agent()),
            asyncio.create_task(self.from_chrome()),
        ]
        self.hold.connections.add(self)
        try:
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception():
                    logger.warning(
                        "slate.browser.proxy: connection ended: %r", task.exception()
                    )
        finally:
            self.hold.connections.discard(self)
            for task in workers:
                task.cancel()
            self.notes.close()
            self.devtools.gone()
            await self.upstream.close()
            await self.client.close()

    async def from_agent(self) -> None:
        async for message in self.client:
            if message.type == WSMsgType.TEXT:
                await self.command(message.data)

    async def from_chrome(self) -> None:
        async for message in self.upstream:
            if message.type != WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            if self.devtools.heard(data):
                continue
            self.follow(data)
            self.notes.heard(data)
            if self.hold.held and "id" not in data:
                self.answer_held(data)
                in_page = bool(data.get("sessionId")) or "" in self.pages
                if in_page and data.get("method") not in HEARD_WHILE_HELD:
                    continue
            self.agent.put(message.data)

    def answer_held(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        session = message.get("sessionId", "")
        if method == "Target.attachedToTarget" and params.get("waitingForDebugger"):
            self.devtools.ask(
                "Runtime.runIfWaitingForDebugger", {}, params["sessionId"]
            )
        elif method == "Fetch.requestPaused":
            answered = "responseStatusCode" in params or "responseErrorReason" in params
            name = "Fetch.continueResponse" if answered else "Fetch.continueRequest"
            self.devtools.ask(name, {"requestId": params["requestId"]}, session)
        elif method == "Fetch.authRequired":
            self.devtools.ask(
                "Fetch.continueWithAuth",
                {
                    "requestId": params["requestId"],
                    "authChallengeResponse": {"response": "Default"},
                },
                session,
            )

    def reaches_page(self, method: str, params: dict, session: str) -> bool:
        return (
            bool(session)
            or "" in self.pages
            or "targetId" in params
            or method in REFUSED_WHILE_HELD
        )

    def back_again(self) -> None:
        for session, page in list(self.pages.items()):
            if session:
                task = asyncio.create_task(self.announce(session, page.target))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def announce(self, session: str, target: str) -> None:
        try:
            found = await self.devtools.call(
                "Target.getTargetInfo", {"targetId": target}
            )
        except CdpError as error:
            logger.warning(
                "slate.browser.proxy: could not hand back %s: %s", target, error
            )
            return
        self.agent.put(
            json.dumps(
                {
                    "method": "Target.attachedToTarget",
                    "params": {
                        "sessionId": session,
                        "targetInfo": found["targetInfo"],
                        "waitingForDebugger": False,
                    },
                }
            )
        )
        for child, heard in list(self.told.items()):
            if self.page_of(child) == session:
                self.agent.put(
                    json.dumps(
                        {
                            "method": "Target.attachedToTarget",
                            "params": {**heard["params"], "waitingForDebugger": False},
                            "sessionId": heard["on"],
                        }
                    )
                )

    def page_of(self, session: str) -> str:
        while session in self.told:
            session = self.told[session]["on"]
        return session

    async def command(self, raw: str) -> None:
        message = json.loads(raw)
        number = message.get("id")
        if not isinstance(number, int):
            logger.warning("slate.browser.proxy: dropped a message without an id")
            return
        method = message.get("method", "")
        params = message.get("params") or {}
        session = message.get("sessionId", "")
        if self.hold.held and self.reaches_page(method, params, session):
            reply = {
                "id": number,
                "error": {"code": -32000, "message": self.hold.taken},
            }
            if session:
                reply["sessionId"] = session
            self.agent.put(json.dumps(reply))
            return
        if method in ("Runtime.enable", "Runtime.disable"):
            self.runtime(raw, number, method, session)
            return
        page = self.pages.get(session)
        if (
            method == "Page.navigate"
            and page
            and params.get("frameId") in (None, page.target)
        ):
            self.toward(session, str(params.get("url", "")))
        waiting = self.notes.held(method, params, session) if page else None
        if waiting:
            await waiting
        self.chrome.put(raw)
        if page:
            self.notes.sent(method, params, session, number)

    def runtime(self, raw: str, number: int, method: str, session: str) -> None:
        page = self.pages.get(session)
        if page:
            page.wanted = method == "Runtime.enable"
        on = bool(page and page.wanted and on_machine(page.url))
        if not page or on == page.on:
            reply = {"id": number, "result": {}}
            if session:
                reply["sessionId"] = session
            self.agent.put(json.dumps(reply))
            return
        page.on = on
        self.chrome.put(raw)

    def toward(self, session: str, url: str) -> None:
        page = self.pages.get(session)
        if not page:
            return
        page.url = url
        on = page.wanted and on_machine(url)
        if on == page.on:
            return
        page.on = on
        self.devtools.ask("Runtime.enable" if on else "Runtime.disable", {}, session)

    def follow(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo", {})
            if info.get("type") == "page":
                self.pages[params["sessionId"]] = Page(
                    info["targetId"], info.get("url", "")
                )
            elif message.get("sessionId"):
                self.told[params["sessionId"]] = {
                    "params": params,
                    "on": message["sessionId"],
                }
            return
        if method == "Target.detachedFromTarget":
            session = params.get("sessionId", "")
            self.pages.pop(session, None)
            self.told.pop(session, None)
            self.devtools.gone(session)
            self.notes.gone(session)
            return
        session = message.get("sessionId", "")
        page = self.pages.get(session)
        if not page:
            return
        if method == "Page.frameNavigated" and not params.get("frame", {}).get(
            "parentId"
        ):
            self.toward(session, params["frame"].get("url", ""))
        elif method in HEADING and params.get("frameId") == page.target:
            self.toward(session, str(params.get("url", "")))
        elif method == "Network.requestWillBeSent" and params.get("type") == "Document":
            if params.get("frameId") == page.target:
                self.toward(session, params.get("request", {}).get("url", ""))


class Proxy:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.hold = Hold(recorder)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/live", self.live)
        app.router.add_get(r"/stills/{number:\d+}", self.still)
        app.router.add_get("/screen", self.screen)
        app.router.add_get("/hold", self.take_over)
        app.router.add_get("/hold/wait", self.wait_for_hand_back)
        app.router.add_get("/hold/watch", self.watch_hold)
        app.router.add_route("*", "/{path:.*}", self.devtools)
        return app

    async def live(self, request: web.Request) -> web.Response:
        seen = request.query.get("version")
        if seen == str(self.recorder.version):
            return web.json_response({"version": self.recorder.version})
        return web.json_response(self.recorder.state())

    async def still(self, request: web.Request) -> web.Response:
        still = self.recorder.stills.get(int(request.match_info["number"]))
        if still is None:
            raise web.HTTPNotFound()
        return web.Response(body=still, content_type="image/jpeg")

    async def screen(self, request: web.Request) -> web.StreamResponse:
        refuse_pages(request)
        async with (
            ClientSession() as session,
            session.ws_connect(SCREEN, max_msg_size=0) as upstream,
        ):
            client = web.WebSocketResponse(max_msg_size=0)
            await client.prepare(request)
            await relay(client, upstream, self.reaches_screen)
            return client

    def reaches_screen(self, message) -> bool:
        if message.type != WSMsgType.TEXT:
            return self.hold.held
        kind = message.data.split(",", 1)[0]
        if self.hold.held:
            self.hold.touched |= kind in SCREEN_TOUCH
            return True
        return kind not in SCREEN_INPUT

    async def take_over(self, request: web.Request) -> web.StreamResponse:
        refuse_pages(request)
        mode = request.query.get("mode", "auto")
        if mode not in HOLD_MODES:
            raise web.HTTPBadRequest(
                text=f"Take over with one of {', '.join(HOLD_MODES)}"
            )
        socket = web.WebSocketResponse(heartbeat=HOLD_HEARTBEAT_SECONDS)
        await socket.prepare(request)
        self.hold.take(mode)
        try:
            await socket.send_str("held")
            async for _ in socket:
                pass
        finally:
            self.hold.release()
        return socket

    async def wait_for_hand_back(self, request: web.Request) -> web.Response:
        refuse_pages(request)
        return web.json_response(await self.hold.wait())

    async def watch_hold(self, request: web.Request) -> web.Response:
        refuse_pages(request)
        paused = request.query.get("paused") == "1"
        return web.json_response(await self.hold.watch(paused))

    async def devtools(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.connect(request)
        async with ClientSession() as session:
            async with session.request(
                request.method,
                f"http://{CHROME}{request.path_qs}",
                data=await request.read(),
            ) as response:
                body = await response.read()
        if request.path.startswith("/json"):
            body = body.decode().replace(CHROME, request.host).encode()
        return web.Response(
            body=body, status=response.status, content_type=response.content_type
        )

    async def connect(self, request: web.Request) -> web.StreamResponse:
        refuse_pages(request)
        target = request.path.rsplit("/", 1)[-1]
        async with ClientSession() as session:
            async with session.ws_connect(
                f"ws://{CHROME}{request.path}", max_msg_size=0
            ) as upstream:
                client = web.WebSocketResponse(max_msg_size=MAX_MESSAGE)
                await client.prepare(request)
                connection = Connection(client, upstream, self.recorder, self.hold)
                if request.path.startswith("/devtools/page/"):
                    connection.pages[""] = Page(target, await page_url(session, target))
                logger.info("slate.browser.proxy: client attached to %s", request.path)
                await connection.run()
                logger.info("slate.browser.proxy: client left %s", request.path)
                return client


def refuse_pages(request: web.Request) -> None:
    if request.headers.get("Origin"):
        logger.warning(
            "slate.browser.proxy: refused a socket from %s", request.headers["Origin"]
        )
        raise web.HTTPForbidden(text="DevTools takes no connections from web pages")


async def page_url(session: ClientSession, target: str) -> str:
    async with session.get(f"http://{CHROME}/json/list") as response:
        pages = await response.json()
    return next((page.get("url", "") for page in pages if page.get("id") == target), "")


async def relay(client: web.WebSocketResponse, upstream, allows) -> None:
    async def pump(source, destination, passes) -> None:
        async for message in source:
            if not passes(message):
                continue
            if message.type == WSMsgType.TEXT:
                await destination.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await destination.send_bytes(message.data)

    pumps = [
        asyncio.create_task(pump(client, upstream, allows)),
        asyncio.create_task(pump(upstream, client, lambda _: True)),
    ]
    try:
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in pumps:
            task.cancel()
        await upstream.close()
        await client.close()
