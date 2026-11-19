import asyncio
import base64
import itertools
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from slate.browser.cdp import CdpError, Devtools, answer

logger = logging.getLogger("slate.browser.actions")

KEPT_ACTIONS = 200
STILL_SECONDS = 0.15
BURST_SECONDS = 1.0
LOOKUP_SECONDS = 1.5
CAPTURE_SECONDS = 1.5
RETAKE_SECONDS = 0.1
QUIET_SECONDS = 0.025
LOAD_SECONDS = 10.0
DRAW_SECONDS = 0.5
HOLD_SECONDS = 0.15
TYPED_CHARACTERS = 200
WINDOW_SECONDS = 2.0
LOAD_WINDOW_SECONDS = 30.0
ERRORS_PER_WINDOW = 10
GROUP = "slate-action"

READS = {
    "Page.captureScreenshot",
    "Page.getFrameTree",
    "Page.getLayoutMetrics",
    "Page.getNavigationHistory",
    "Page.getResourceTree",
    "Accessibility.getFullAXTree",
    "Accessibility.getPartialAXTree",
    "Accessibility.queryAXTree",
    "Accessibility.getChildAXNodes",
    "Accessibility.getRootAXNode",
    "DOM.getDocument",
    "DOM.describeNode",
    "DOM.resolveNode",
    "DOM.requestNode",
    "DOM.querySelector",
    "DOM.querySelectorAll",
    "DOM.getAttributes",
    "DOM.getOuterHTML",
    "DOM.getBoxModel",
    "DOM.getContentQuads",
    "DOM.getNodeForLocation",
    "DOMSnapshot.captureSnapshot",
    "Runtime.getProperties",
    "Runtime.releaseObject",
    "Runtime.releaseObjectGroup",
    "Target.getTargetInfo",
    "Target.getTargets",
}
NAMED_KEYS = {
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
}
MODIFIERS = ("Alt", "Control", "Meta", "Shift")
FIELD_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
CHOICE_ROLES = {"combobox", "listbox"}
CONTROL_ROLES = {
    "button",
    "link",
    "listbox",
    "option",
    "checkbox",
    "radio",
    "switch",
    "slider",
    "tab",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "treeitem",
}
SECRET_WORDS = (
    "cardnumber",
    "card-number",
    "card_number",
    "ccnumber",
    "cc-number",
    "cc_number",
    "cvc",
    "cvv",
    "securitycode",
    "security-code",
    "security_code",
    "onetimecode",
    "one-time-code",
    "one_time_code",
)
SECRET_TOKENS = (
    "cc-number",
    "cc-exp",
    "cc-exp-month",
    "cc-exp-year",
    "cc-csc",
    "one-time-code",
    "current-password",
    "new-password",
)
SECRET_FIELDS = ",".join(
    [
        "input[type=password i]",
        *(f"input[autocomplete~={token} i]" for token in SECRET_TOKENS),
        *(
            selector
            for attribute in ("name", "id")
            for selector in (
                *(f"input[{attribute}*={word} i]" for word in SECRET_WORDS),
                f"input[{attribute}=otp i]",
                f"input[{attribute}^=otp i]",
                f"input[{attribute}$=otp i]",
            )
        ),
        "input[data-slate-secret]",
    ]
)
FOCUSED = """(() => {
  let element = document.activeElement;
  for (;;) {
    const inner =
      element?.shadowRoot?.activeElement ?? element?.contentDocument?.activeElement;
    if (!inner) return element;
    element = inner;
  }
})()"""
PLAIN_FIELD = """function (secret) {
  const field = this.matches?.("input, textarea") || this.isContentEditable === true;
  return field && !this.matches(secret);
}"""
CHOSEN = """function () {
  if (this.matches?.('select[autocomplete*="cc-" i]')) return "";
  return [...(this.selectedOptions ?? [])]
    .map((option) => option.label || option.textContent || "")
    .map((text) => text.replace(/\\s+/g, " ").trim())
    .filter(Boolean)
    .join(", ");
}"""


class Recorder:
    def __init__(self) -> None:
        self.numbers = itertools.count(1)
        self.actions: dict[int, dict] = {}
        self.stills: dict[int, bytes] = {}
        self.errors: list[dict] = []
        self.page: dict | None = None
        self.held = False
        self.trail: list[dict] = []
        self.version = 0
        self.window: dict | None = None

    def begin(self, at: float) -> int:
        number = next(self.numbers)
        window = self.open_window(at)
        if window:
            window["last"] = number
            window["until"] = max(window["until"], at + WINDOW_SECONDS)
        else:
            self.window = {
                "until": at + WINDOW_SECONDS,
                "last": number,
                "load_from": None,
                "loading": False,
                "kept": set(),
            }
        return number

    def keep(self, number: int, action: dict, still: bytes | None) -> None:
        action["id"] = number
        action["still"] = still is not None
        self.actions[number] = action
        if still is not None:
            self.stills[number] = still
        for old in sorted(self.actions)[:-KEPT_ACTIONS]:
            self.actions.pop(old)
            self.stills.pop(old, None)
        oldest = min(self.actions)
        self.errors = [error for error in self.errors if error["after"] >= oldest]
        self.changed()

    def show(self, page: dict | None) -> None:
        if page != self.page:
            self.page = page
            if self.held and page:
                self.follow(page)
            self.changed()

    def follow(self, page: dict) -> None:
        if self.trail and self.trail[-1]["url"] == page["url"]:
            self.trail[-1] = dict(page)
        else:
            self.trail.append(dict(page))

    def holding(self, held: bool) -> None:
        self.held = held
        if held:
            self.trail = [dict(self.page)] if self.page else []
        kind = "take over" if held else "hand back"
        action = {"kind": kind, "at": round(time.time() * 1000)}
        self.keep(next(self.numbers), action, None)

    def changed(self) -> None:
        self.version += 1

    def state(self) -> dict:
        return {
            "version": self.version,
            "page": self.page,
            "held": self.held,
            "actions": [self.actions[number] for number in sorted(self.actions)],
            "errors": self.errors,
        }

    def open_window(self, at: float) -> dict | None:
        window = self.window
        if window and (window["loading"] or at <= window["until"]):
            return window
        self.window = None
        return None

    def loading(self, started: bool) -> None:
        now = time.time()
        window = self.open_window(now)
        if not window:
            return
        if started:
            window["loading"] = True
            window["load_from"] = window["last"]
            window["until"] = max(window["until"], now + LOAD_WINDOW_SECONDS)
        elif window["loading"]:
            window["loading"] = False
            window["until"] = now + WINDOW_SECONDS

    def error(self, noticed: dict) -> None:
        window = self.open_window(noticed["at"])
        if not window or len(window["kept"]) >= ERRORS_PER_WINDOW:
            return
        said = json.dumps({**noticed, "at": 0}, sort_keys=True)
        if said in window["kept"]:
            return
        window["kept"].add(said)
        after = window["load_from"] if window["loading"] else window["last"]
        self.errors.append({**noticed, "after": after})
        self.changed()


@dataclass
class Typing:
    text: str
    unanswered: set[int]
    by_key: bool
    still: asyncio.Task | None = None
    quiet: asyncio.TimerHandle | None = None
    held: bool = False


@dataclass
class Burst:
    kind: str
    last: float
    typing: Typing | None = None


@dataclass
class Pending:
    number: int
    made: asyncio.Task
    typing: Typing | None


@dataclass
class Load:
    done: asyncio.Event = field(default_factory=asyncio.Event)
    drawing: asyncio.TimerHandle | None = None


@dataclass
class Tab:
    actions: list[Pending] = field(default_factory=list)
    still: asyncio.TimerHandle | None = None
    burst: Burst | None = None
    load: Load | None = None


class Notes:
    def __init__(self, devtools: Devtools, recorder: Recorder, targets: dict) -> None:
        self.devtools = devtools
        self.recorder = recorder
        self.targets = targets
        self.tabs: dict[str, Tab] = {}
        self.answers: dict[tuple[str, int], asyncio.Event] = {}
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, work) -> asyncio.Task:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def held(self, method: str, params: dict, session: str):
        tab = self.tabs.get(session)
        burst = tab.burst if tab else None
        typing = burst.typing if burst else None
        if not typing or typing.held or method in READS:
            return None
        if method == "Input.dispatchKeyEvent" and params.get("type") == "keyUp":
            return None
        if (
            kind_of(method, params) == "type"
            and time.time() - burst.last < BURST_SECONDS
        ):
            return None
        if typing.quiet:
            self.still_of_typing(session, typing)
        if not typing.still:
            return None
        typing.held = True
        return capped(typing.still, HOLD_SECONDS)

    def sent(self, method: str, params: dict, session: str, number: int) -> None:
        if method == "Input.dispatchKeyEvent" and params.get("type") == "keyUp":
            return
        at = time.time()
        tab = self.tabs.setdefault(session, Tab())
        kind = kind_of(method, params)
        if kind and tab.load and tab.load.drawing:
            self.drawn(tab, tab.load)
        typed = params.get("text", "") if kind == "type" else ""
        typed = typed if isinstance(typed, str) else ""
        burst = tab.burst
        if (
            burst
            and kind == burst.kind
            and at - burst.last < BURST_SECONDS
            and (not burst.typing or any(p.typing is burst.typing for p in tab.actions))
        ):
            burst.last = at
            if burst.typing:
                burst.typing.text = (burst.typing.text + typed)[:TYPED_CHARACTERS]
                burst.typing.unanswered.add(number)
                burst.typing.by_key = method == "Input.dispatchKeyEvent"
                cancel(burst.typing.quiet)
                burst.typing.quiet = None
            if tab.actions:
                self.still_later(session, tab)
            return
        if not kind:
            tab.burst = None
            return
        action_number = self.recorder.begin(at)
        typing = None
        if kind == "type":
            typing = Typing(
                typed[:TYPED_CHARACTERS], {number}, method == "Input.dispatchKeyEvent"
            )
        tab.burst = Burst(kind, at, typing) if kind in ("type", "scroll") else None
        answered = asyncio.Event()
        self.answers[(session, number)] = answered
        asyncio.get_running_loop().call_later(LOOKUP_SECONDS, answered.set)
        made = self.spawn(self.action_of(kind, method, params, at, session, answered))
        tab.actions.append(Pending(action_number, made, typing))
        self.still_later(session, tab)

    def heard(self, message: dict) -> None:
        session = message.get("sessionId", "")
        if session not in self.targets:
            return
        number = message.get("id")
        if isinstance(number, int):
            self.answered(session, number)
            return
        method = message.get("method", "")
        params = message.get("params") or {}
        main = params.get("frameId") in (None, self.targets[session].target)
        if method == "Page.frameStartedLoading" and main:
            self.loading(session, True)
        elif method in ("Page.frameStoppedLoading", "Page.loadEventFired") and main:
            self.loading(session, False)

    def gone(self, session: str) -> None:
        tab = self.tabs.pop(session, None)
        if not tab:
            return
        if tab.load:
            self.drawn(tab, tab.load)
        self.keep(session, tab, False)

    def close(self) -> None:
        for session in list(self.tabs):
            self.gone(session)

    def answered(self, session: str, number: int) -> None:
        event = self.answers.pop((session, number), None)
        if event:
            event.set()
        tab = self.tabs.get(session)
        for pending in tab.actions if tab else []:
            typing = pending.typing
            if typing and number in typing.unanswered:
                typing.unanswered.discard(number)
                if typing.unanswered:
                    continue
                cancel(typing.quiet)
                if typing.by_key:
                    typing.quiet = asyncio.get_running_loop().call_later(
                        QUIET_SECONDS, self.still_of_typing, session, typing
                    )
                else:
                    self.still_of_typing(session, typing)

    def loading(self, session: str, started: bool) -> None:
        tab = self.tabs.setdefault(session, Tab())
        if started and not tab.load:
            tab.load = Load()
        load = tab.load
        if not load:
            return
        cancel(load.drawing)
        load.drawing = None
        if not started:
            load.drawing = asyncio.get_running_loop().call_later(
                DRAW_SECONDS, self.drawn, tab, load
            )

    def drawn(self, tab: Tab, load: Load) -> None:
        cancel(load.drawing)
        load.drawing = None
        load.done.set()
        if tab.load is load:
            tab.load = None

    def still_later(self, session: str, tab: Tab) -> None:
        cancel(tab.still)
        tab.still = asyncio.get_running_loop().call_later(
            STILL_SECONDS, self.keep, session, tab, True
        )

    def still_of_typing(self, session: str, typing: Typing) -> None:
        cancel(typing.quiet)
        typing.quiet = None
        typing.still = self.spawn(self.after(typing.still, session))

    async def after(self, previous: asyncio.Task | None, session: str) -> bytes | None:
        if previous:
            await asyncio.wait([previous])
        return await self.capture(session)

    def keep(self, session: str, tab: Tab, still: bool) -> None:
        cancel(tab.still)
        actions, tab.actions = tab.actions, []
        if not actions:
            return
        shared = None
        if still and any(not (p.typing and p.typing.still) for p in actions):
            shared = self.spawn(self.loaded_still(session, tab))
        for pending in actions:
            self.spawn(self.finish(pending, shared))

    async def finish(self, pending: Pending, shared: asyncio.Task | None) -> None:
        try:
            action = await pending.made
            picture = None
            if pending.typing and pending.typing.still:
                picture = await pending.typing.still
            if picture is None and shared:
                picture = await shared
        except Exception:
            logger.exception(
                "slate.browser.actions: could not finish action %s", pending.number
            )
            return
        if pending.typing:
            plain = action.pop("plain", False)
            if plain and pending.typing.text:
                action["text"] = pending.typing.text
        self.recorder.keep(pending.number, action, picture)

    async def loaded_still(self, session: str, tab: Tab) -> bytes | None:
        load = tab.load
        if load:
            await capped(load.done.wait(), LOAD_SECONDS)
            self.drawn(tab, load)
        return await self.capture(session)

    async def capture(self, session: str) -> bytes | None:
        deadline = time.monotonic() + CAPTURE_SECONDS
        while time.monotonic() < deadline:
            try:
                shot = await self.devtools.call(
                    "Page.captureScreenshot",
                    {"format": "jpeg", "quality": 70},
                    session,
                    deadline - time.monotonic(),
                )
                return base64.b64decode(shot["data"])
            except CdpError as error:
                if session not in self.targets:
                    return None
                logger.debug("slate.browser.actions: still failed, retaking: %s", error)
                await asyncio.sleep(RETAKE_SECONDS)
        logger.warning("slate.browser.actions: no still of %s in time", session[:8])
        return None

    async def action_of(
        self,
        kind: str,
        method: str,
        params: dict,
        at: float,
        session: str,
        answered: asyncio.Event,
    ) -> dict:
        action = {"kind": kind, "at": round(at * 1000)}
        if kind == "click":
            point = {"x": number(params.get("x")), "y": number(params.get("y"))}
            action["point"] = point
            located = self.devtools.ask(
                "DOM.getNodeForLocation",
                {
                    "x": round(point["x"]),
                    "y": round(point["y"]),
                    "ignorePointerEventsNone": True,
                },
                session,
            )
            return action | await self.lookup(self.control_at(session, located))
        if kind == "type":
            return action | await self.lookup(self.focused(session))
        if kind == "choose":
            described = self.devtools.ask(
                "DOM.describeNode", {"objectId": params["objectId"]}, session
            )
            return action | await self.lookup(self.chose(session, described, answered))
        if kind == "key":
            return action | {"key": chord(params)}
        if kind == "open":
            return action | {"page": page_of(params.get("url", ""))}
        return action

    async def lookup(self, work) -> dict:
        try:
            return await asyncio.wait_for(work, LOOKUP_SECONDS)
        except (CdpError, TimeoutError, KeyError, TypeError) as error:
            logger.debug("slate.browser.actions: lookup failed: %s", error)
            return {}

    async def control_at(self, session: str, located: asyncio.Future) -> dict:
        node = (await answer(located, "DOM.getNodeForLocation", LOOKUP_SECONDS))[
            "backendNodeId"
        ]
        tree = await self.devtools.call(
            "Accessibility.getPartialAXTree",
            {"backendNodeId": node, "fetchRelatives": True},
            session,
        )
        nodes = tree.get("nodes", [])
        by_id = {each["nodeId"]: each for each in nodes}
        current = next(
            (each for each in nodes if each.get("backendDOMNodeId") == node), None
        )
        for _ in range(12):
            if current is None:
                return {}
            role = role_of(current)
            name = name_of(current)
            if role in FIELD_ROLES or (role in CONTROL_ROLES and name):
                found = {"control": {"role": role, "name": name}}
                box = await self.box_of(session, current.get("backendDOMNodeId"))
                return found | ({"box": box} if box else {})
            current = by_id.get(current.get("parentId"))
        return {}

    async def focused(self, session: str) -> dict:
        evaluated = await self.devtools.call(
            "Runtime.evaluate", {"expression": FOCUSED, "objectGroup": GROUP}, session
        )
        try:
            object_id = evaluated.get("result", {}).get("objectId")
            if not object_id:
                return {"plain": False}
            checked = await self.devtools.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": PLAIN_FIELD,
                    "arguments": [{"value": SECRET_FIELDS}],
                    "returnByValue": True,
                },
                session,
            )
            plain = checked.get("result", {}).get("value") is True
            described = await self.devtools.call(
                "DOM.describeNode", {"objectId": object_id}, session
            )
            node = described["node"]["backendNodeId"]
            found = {"plain": plain} | await self.named(session, node, FIELD_ROLES)
            box = await self.box_of(session, node)
            return found | ({"box": box} if box else {})
        finally:
            self.devtools.ask(
                "Runtime.releaseObjectGroup", {"objectGroup": GROUP}, session
            )

    async def chose(
        self, session: str, described: asyncio.Future, answered: asyncio.Event
    ) -> dict:
        node = (await answer(described, "DOM.describeNode", LOOKUP_SECONDS))["node"][
            "backendNodeId"
        ]
        await answered.wait()
        resolved = await self.devtools.call(
            "DOM.resolveNode", {"backendNodeId": node, "objectGroup": GROUP}, session
        )
        try:
            shown = await self.devtools.call(
                "Runtime.callFunctionOn",
                {
                    "objectId": resolved["object"]["objectId"],
                    "functionDeclaration": CHOSEN,
                    "returnByValue": True,
                },
                session,
            )
        finally:
            self.devtools.ask(
                "Runtime.releaseObjectGroup", {"objectGroup": GROUP}, session
            )
        found = await self.named(session, node, CHOICE_ROLES)
        option = shown.get("result", {}).get("value")
        if isinstance(option, str) and option:
            found["option"] = option[:TYPED_CHARACTERS]
        box = await self.box_of(session, node)
        return found | ({"box": box} if box else {})

    async def named(self, session: str, node: int, roles: set[str]) -> dict:
        tree = await self.devtools.call(
            "Accessibility.getPartialAXTree",
            {"backendNodeId": node, "fetchRelatives": False},
            session,
        )
        own = next(
            (
                each
                for each in tree.get("nodes", [])
                if each.get("backendDOMNodeId") == node
            ),
            None,
        )
        if own is None or role_of(own) not in roles or not name_of(own):
            return {}
        return {"control": {"role": role_of(own), "name": name_of(own)}}

    async def box_of(self, session: str, node: int | None) -> dict | None:
        if node is None:
            return None
        try:
            quads = (
                await self.devtools.call(
                    "DOM.getContentQuads", {"backendNodeId": node}, session
                )
            )["quads"]
        except (CdpError, KeyError):
            return None
        if not quads or len(quads[0]) < 8:
            return None
        xs, ys = quads[0][0::2], quads[0][1::2]
        left, top = min(xs), min(ys)
        return {
            "x": round(left, 1),
            "y": round(top, 1),
            "width": round(max(xs) - left, 1),
            "height": round(max(ys) - top, 1),
        }


def kind_of(method: str, params: dict) -> str | None:
    if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed":
        return "click"
    if method == "Input.insertText" or (
        method == "Input.dispatchKeyEvent" and typing(params)
    ):
        return "type"
    if method == "Input.dispatchKeyEvent" and params.get("type") in (
        "keyDown",
        "rawKeyDown",
    ):
        return "key" if params.get("key") in NAMED_KEYS else None
    if method == "Page.navigate":
        return "open" if page_of(params.get("url", "")) else None
    declaration = str(params.get("functionDeclaration", ""))
    if (
        method == "Runtime.callFunctionOn"
        and isinstance(params.get("objectId"), str)
        and "this.options" in declaration
        and re.search(r"\.selected\s*=", declaration)
    ):
        return "choose"
    scrolls = (
        (method == "Input.dispatchMouseEvent" and params.get("type") == "mouseWheel")
        or method == "Input.synthesizeScrollGesture"
        or (
            method == "Runtime.evaluate"
            and re.match(r"\s*window\.scrollBy\(", str(params.get("expression", "")))
        )
        or (method == "Runtime.callFunctionOn" and "this.scrollBy(" in declaration)
    )
    return "scroll" if scrolls else None


def typing(params: dict) -> bool:
    text = params.get("text") if isinstance(params.get("text"), str) else ""
    shortcut = int(params.get("modifiers") or 0) & (2 | 4)
    return (
        params.get("type") in ("keyDown", "char")
        and not shortcut
        and params.get("key") not in NAMED_KEYS
        and len(text) > 0
        and ord(text[0]) >= 32
    )


def chord(params: dict) -> str:
    key = str(params.get("key"))
    modifiers = int(params.get("modifiers") or 0)
    held = [name for bit, name in enumerate(MODIFIERS) if modifiers & (1 << bit)]
    joined = "+".join([*held, key])
    return joined if len(joined) <= 24 else key


def page_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.netloc or parts.scheme == "file":
        return f"{parts.scheme}://{parts.netloc}{parts.path}"[:400]
    if parts.scheme == "about":
        return f"about:{parts.path}"
    return parts.scheme or None


def role_of(node: dict) -> str:
    return str((node.get("role") or {}).get("value") or "")


def name_of(node: dict) -> str:
    return " ".join(str((node.get("name") or {}).get("value") or "").split())[:120]


def number(value) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def cancel(handle: asyncio.TimerHandle | None) -> None:
    if handle:
        handle.cancel()


async def capped(work, seconds: float) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(work)), seconds)
    except (TimeoutError, CdpError):
        pass
