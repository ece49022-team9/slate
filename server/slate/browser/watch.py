import asyncio
import json
import logging
import time
from urllib.parse import urlsplit

from aiohttp import ClientSession, WSMsgType

from slate.browser.actions import SECRET_FIELDS, Recorder
from slate.browser.cdp import CdpError, Devtools, Outbox
from slate.browser.proxy import CHROME, on_machine

logger = logging.getLogger("slate.browser.watch")
COUNTED = {"Document", "Script", "Fetch", "XHR", "Image"}
IN_FLIGHT = 1000
MESSAGE_CHARACTERS = 300
REFRESH_SECONDS = 1.0
COVER_WORLD = "slateCover"
AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
COVER_STYLE = json.dumps(SECRET_FIELDS + "{-webkit-text-security:disc!important}")
COVER = (
    """(() => {
  if (globalThis.covering) return;
  globalThis.covering = true;
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(STYLE);
  const adopt = () => {
    if (!document.adoptedStyleSheets.includes(sheet))
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  };
  adopt();
  for (const event of ["input", "focusin"]) addEventListener(event, adopt, true);
  new MutationObserver((changes) => {
    adopt();
    for (const { type, target, oldValue } of changes)
      if (
        type === "attributes" &&
        oldValue?.toLowerCase() === "password" &&
        target.type !== "password"
      )
        target.setAttribute("data-slate-secret", "");
  }).observe(document, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["type"],
    attributeOldValue: true,
  });
})();"""
).replace("STYLE", COVER_STYLE)


class Guarded:
    def __init__(
        self, devtools: Devtools, recorder: Recorder, session: str, target: str
    ) -> None:
        self.devtools = devtools
        self.recorder = recorder
        self.session = session
        self.target = target
        self.frames: set[str] = set()
        self.address = ""
        self.own = False
        self.runtime = False
        self.requests: dict[str, dict] = {}
        self.reading: asyncio.Task | None = None

    def start(self) -> None:
        self.cover(self.session)
        self.devtools.ask(
            "Network.enable",
            {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0},
            self.session,
        )
        self.devtools.ask("Inspector.enable", {}, self.session)
        self.reading = asyncio.create_task(self.first_address())

    def cover(self, session: str) -> None:
        self.devtools.ask("Page.enable", {}, session)
        self.devtools.ask(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": COVER, "worldName": COVER_WORLD, "runImmediately": True},
            session,
        )
        self.devtools.ask("Target.setAutoAttach", AUTO_ATTACH, session)

    async def first_address(self) -> None:
        try:
            tree = await self.devtools.call("Page.getFrameTree", {}, self.session)
        except CdpError as error:
            logger.debug(
                "slate.browser.watch: no frame tree for %s: %s", self.target[:8], error
            )
            return
        self.follow(tree["frameTree"]["frame"].get("url", ""))

    def follow(self, url: str) -> None:
        self.address = address_of(url) or self.address
        self.own = on_machine(url)
        if self.own == self.runtime:
            return
        self.runtime = self.own
        self.devtools.ask(
            "Runtime.enable" if self.own else "Runtime.disable", {}, self.session
        )

    def child(self, params: dict) -> None:
        session = params["sessionId"]
        if params.get("targetInfo", {}).get("type") == "iframe":
            self.frames.add(session)
            self.cover(session)
        if params.get("waitingForDebugger"):
            self.devtools.ask("Runtime.runIfWaitingForDebugger", {}, session)

    def heard(self, method: str, params: dict, session: str) -> None:
        if method == "Target.attachedToTarget":
            self.child(params)
            return
        if method == "Target.detachedFromTarget":
            self.frames.discard(params.get("sessionId", ""))
            return
        if session != self.session:
            return
        now = time.time()
        if method == "Page.frameNavigated" and not params["frame"].get("parentId"):
            self.follow(params["frame"].get("url", ""))
        elif (
            method == "Page.frameStartedNavigating"
            and params.get("frameId") == self.target
        ):
            self.follow(str(params.get("url", "")))
        elif (
            method == "Page.frameStartedLoading"
            and params.get("frameId") == self.target
        ):
            self.recorder.loading(True)
        elif method == "Page.loadEventFired":
            self.recorder.loading(False)
        elif method == "Network.requestWillBeSent":
            request = params["request"]
            self.requests[params["requestId"]] = {
                "method": request.get("method", ""),
                "url": request.get("url", ""),
            }
            if len(self.requests) > IN_FLIGHT:
                self.requests.pop(next(iter(self.requests)))
        elif method == "Network.responseReceived":
            status = params["response"].get("status", 0)
            failed = (
                self.failed(params["requestId"], params.get("type", ""))
                if 400 <= status <= 599
                else None
            )
            if failed:
                self.keep({"kind": "response", "at": now, **failed, "status": status})
        elif method == "Network.loadingFinished":
            self.requests.pop(params.get("requestId"), None)
        elif method == "Network.loadingFailed":
            self.lost(params, now)
        elif method == "Inspector.targetCrashed":
            self.keep({"kind": "crash", "at": now})
        elif method == "Runtime.exceptionThrown" and self.own:
            details = params.get("exceptionDetails", {})
            description = (
                (details.get("exception") or {}).get("description", "").split("\n")[0]
            )
            self.keep(
                {
                    "kind": "exception",
                    "at": now,
                    "message": message_of(
                        f"{details.get('text', 'Uncaught')} {description}"
                    ),
                }
            )
        elif (
            method == "Runtime.consoleAPICalled"
            and self.own
            and params.get("type") == "error"
        ):
            words = [argument_text(argument) for argument in params.get("args", [])]
            self.keep(
                {"kind": "console", "at": now, "message": message_of(" ".join(words))}
            )

    def lost(self, params: dict, now: float) -> None:
        request_id = params.get("requestId")
        ignored = params.get("canceled") or params.get("blockedReason")
        if ignored or params.get("errorText") == "net::ERR_ABORTED":
            self.requests.pop(request_id, None)
            return
        failed = self.failed(request_id, params.get("type", ""))
        if failed:
            reason = str(params.get("errorText", ""))[:120]
            self.keep({"kind": "request", "at": now, **failed, "reason": reason})

    def failed(self, request_id: str, kind: str) -> dict | None:
        request = self.requests.pop(request_id, None)
        if not request or kind not in COUNTED:
            return None
        if kind != "Document" and site_of(request["url"]) != site_of(self.address):
            return None
        url = address_of(request["url"])
        return {"method": request["method"], "url": url} if url else None

    def keep(self, noticed: dict) -> None:
        if self.address:
            self.recorder.error({**noticed, "page": self.address})


class Watch:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.guarded: dict[str, Guarded] = {}
        self.pages: dict[str, dict] = {}
        self.front: str | None = None

    async def run(self) -> None:
        while True:
            try:
                await self.watch()
            except (OSError, CdpError, KeyError) as error:
                logger.warning(
                    "slate.browser.watch: lost Chrome (%r); watching again", error
                )
            self.guarded.clear()
            self.pages.clear()
            self.front = None
            self.recorder.show(None)
            await asyncio.sleep(1)

    async def watch(self) -> None:
        async with ClientSession() as session:
            async with session.get(f"http://{CHROME}/json/version") as response:
                address = (await response.json())["webSocketDebuggerUrl"]
            async with session.ws_connect(address, max_msg_size=0) as socket:
                outbox = Outbox(socket.send_str)
                writer = asyncio.create_task(outbox.run())
                devtools = Devtools(outbox.put, 1)
                reader = asyncio.create_task(self.read(socket, devtools))
                refresher = asyncio.create_task(self.refresh(devtools))
                try:
                    await devtools.call("Target.setDiscoverTargets", {"discover": True})
                    await devtools.call("Target.setAutoAttach", AUTO_ATTACH)
                    logger.info(
                        "slate.browser.watch: guarding %d pages", len(self.guarded)
                    )
                    await reader
                finally:
                    reader.cancel()
                    refresher.cancel()
                    writer.cancel()
                    devtools.gone()

    async def refresh(self, devtools: Devtools) -> None:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            front = self.front
            if not front:
                continue
            try:
                found = await devtools.call("Target.getTargetInfo", {"targetId": front})
            except CdpError:
                continue
            if front == self.front:
                self.pages[front] = described(found["targetInfo"])
                self.recorder.show(self.pages[front])

    async def read(self, socket, devtools: Devtools) -> None:
        async for message in socket:
            if message.type != WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            if devtools.heard(data):
                continue
            method = data.get("method", "")
            params = data.get("params") or {}
            session = data.get("sessionId", "")
            if not session:
                self.browser_event(devtools, method, params)
                continue
            owner = self.owner(session)
            if owner:
                owner.heard(method, params, session)

    def owner(self, session: str) -> Guarded | None:
        if session in self.guarded:
            return self.guarded[session]
        return next(
            (each for each in self.guarded.values() if session in each.frames), None
        )

    def browser_event(self, devtools: Devtools, method: str, params: dict) -> None:
        if method == "Target.attachedToTarget":
            info = params["targetInfo"]
            session = params["sessionId"]
            if info.get("type") == "page":
                guarded = Guarded(devtools, self.recorder, session, info["targetId"])
                self.guarded[session] = guarded
                guarded.start()
            if params.get("waitingForDebugger"):
                devtools.ask("Runtime.runIfWaitingForDebugger", {}, session)
        elif method == "Target.detachedFromTarget":
            self.guarded.pop(params.get("sessionId", ""), None)
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = params["targetInfo"]
            if info.get("type") != "page":
                return
            self.pages[info["targetId"]] = described(info)
            self.front = info["targetId"]
            self.recorder.show(self.pages[self.front])
        elif method == "Target.targetDestroyed":
            self.pages.pop(params["targetId"], None)
            if params["targetId"] == self.front:
                self.front = next(reversed(self.pages), None)
                self.recorder.show(self.pages.get(self.front) if self.front else None)


def described(info: dict) -> dict:
    return {"url": info.get("url", ""), "title": info.get("title", "")}


def address_of(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:400]


def site_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    labels = host.split(".")
    if host.replace(".", "").isdigit() or len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def argument_text(argument: dict) -> str:
    value = argument.get("value")
    if isinstance(value, str):
        return value
    return str(argument.get("description", value or "")).split("\n")[0]


def message_of(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= MESSAGE_CHARACTERS else line[:MESSAGE_CHARACTERS] + "…"
