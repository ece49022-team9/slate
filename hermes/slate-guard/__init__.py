import hashlib
import json
import logging
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from hermes_cli.config import load_config_readonly
from tools import browser_tool, browser_tool_lifecycle, browser_tool_session

from .policy import review

logger = logging.getLogger("slate.guard")
TAKEN = "taken over the browser"
GONE = ("Could not locate element", "Unknown ref")
READS = ("browser_snapshot", "browser_vision", "browser_get_images", "browser_console")
VAULT = "browser_vault_"
LONGEST_WAIT_SECONDS = 540
SLICE_SECONDS = 35
KEPT = (
    "The user has kept the browser for over 9 minutes, so this did not run. "
    "Tell them where you got to and that you'll carry on once they hand it back."
)
WATCH_SECONDS = 35
STALE_SECONDS = 40


class Taken(Exception):
    pass


class Gone(Exception):
    pass


def hold_url(path: str, query: str = "") -> str:
    parts = urlsplit(load_config_readonly()["browser"]["cdp_url"])
    scheme = "https" if parts.scheme == "wss" else "http"
    extra = f"&{query}" if query else ""
    return f"{scheme}://{parts.netloc}/hold/{path}?{parts.query}{extra}"


class HoldWatch:
    def __init__(self) -> None:
        self.paused = False
        self.heard = float("-inf")
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    @property
    def free(self) -> bool:
        fresh = time.monotonic() - self.heard < STALE_SECONDS
        return fresh and not self.paused

    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(
                target=self.run, name="slate-hold-watch", daemon=True
            )
            self.thread.start()

    def run(self) -> None:
        while True:
            url = hold_url("watch", f"paused={int(self.paused)}")
            try:
                with urllib.request.urlopen(url, timeout=WATCH_SECONDS) as response:
                    self.paused = json.loads(response.read())["paused"]
                self.heard = time.monotonic()
            except (OSError, KeyError, ValueError) as error:
                logger.warning("slate.guard: lost the hold watch: %r", error)
                time.sleep(1)


watch = HoldWatch()


def wait_for_hand_back(tool_name: str, task_id: str) -> dict:
    started = time.monotonic()
    key = browser_tool._last_session_key(task_id or "default")
    while True:
        with urllib.request.urlopen(
            hold_url("wait"), timeout=SLICE_SECONDS
        ) as response:
            handed = json.loads(response.read())
        if handed["then"] != "held":
            break
        browser_tool_lifecycle._update_session_activity(key)
        if time.monotonic() - started > LONGEST_WAIT_SECONDS:
            logger.warning(
                "slate.guard: the user kept the browser; %s gives up", tool_name
            )
            handed = {"then": "gave up"}
            break
    waited = time.monotonic() - started
    logger.info(
        "slate.guard: hold check for %s took %d ms (%s)",
        tool_name,
        waited * 1000,
        handed["then"],
    )
    return handed


def named(page: dict | None) -> str:
    if not page:
        return "no page"
    return f'"{page.get("title") or page.get("url")}" ({page.get("url")})'


def told(tool_name: str, handed: dict) -> str:
    before, after = handed.get("before"), handed.get("after")
    lines = [
        f"The user had the browser for {handed['seconds']:.0f} s and handed it back, "
        f"so your {tool_name} did not run."
    ]
    if (before or {}).get("url") == (after or {}).get("url"):
        lines.append(f"The page is still {named(after)}.")
    else:
        lines.append(f"The page went from {named(before)} to {named(after)}.")
    between = [named(page) for page in handed.get("visited", [])[1:-1]]
    if between:
        lines.append("On the way they opened " + "; ".join(between) + ".")
    return " ".join(lines)


def after_hand_back(tool_name: str, task_id: str) -> dict | None:
    watch.start()
    if watch.free:
        return None
    handed = wait_for_hand_back(tool_name, task_id)
    then = handed["then"]
    if then == "gave up":
        return {"action": "block", "message": KEPT}
    if then == "auto":
        then = "brief" if changed(handed) else "resume"
    if then in ("go", "refuse", "resume") or tool_name in READS:
        return None
    message = told(tool_name, handed)
    if then == "brief":
        message += (
            " Refs from your earlier reads may point at different elements now. "
            "Read the page again, then carry on with the task."
        )
    else:
        message += (
            " Here is the page as it is now; these refs are current. "
            "Carry on with the task from here.\n"
            + browser_tool.browser_snapshot(task_id=task_id)
        )
    logger.info("slate.guard: %s told the user handed back (%s)", tool_name, then)
    return {"action": "block", "message": message}


def changed(handed: dict) -> bool:
    before, after = handed.get("before") or {}, handed.get("after") or {}
    moved = before.get("url") != after.get("url") or len(handed.get("visited", [])) > 1
    return moved or handed.get("touched", True)


def browser_get(key: str, query: list[str]) -> dict:
    result = browser_tool_session._run_browser_command(key, "get", query, timeout=5)
    if not result.get("success"):
        error = str(result.get("error") or f"browser get {query[0]} failed")
        if TAKEN in error:
            raise Taken(error)
        if any(sign in error for sign in GONE):
            raise Gone(error)
        raise LookupError(error)
    return result.get("data") or {}


def page(task_id: str, ref: str | None) -> tuple[str, str]:
    key = browser_tool._last_session_key(task_id or "default")
    label = []
    if ref:
        target = browser_tool._at_ref(ref)
        for query in (
            ["text", target],
            ["value", target],
            ["attr", target, "aria-label"],
        ):
            try:
                data = browser_get(key, query)
            except LookupError:
                if query[0] == "text":
                    raise
                continue
            label.append(str(data.get("text" if query[0] == "text" else "value") or ""))
    url = browser_get(key, ["url"]).get("url", "")
    return " ".join(label), str(url)


def guard(
    tool_name: str, args: dict | None = None, task_id: str = "", **_
) -> dict | None:
    if tool_name.startswith("browser_") and not tool_name.startswith(VAULT):
        try:
            directive = after_hand_back(tool_name, task_id)
        except OSError as error:
            logger.warning(
                "slate.guard: could not ask whether the user has the browser: %r", error
            )
            directive = None
        if directive:
            return directive
    try:
        reason = review(tool_name, args or {}, lambda ref: page(task_id, ref))
    except Taken as error:
        logger.info("slate.guard: %s waits, the user has the browser", tool_name)
        return {"action": "block", "message": str(error)}
    except Gone as error:
        logger.info("slate.guard: %s aimed at a stale ref: %s", tool_name, error)
        return {
            "action": "block",
            "message": f"{error}. The page changed since you last read it, "
            "so this did not run. Read the page again and use its new refs.",
        }
    except Exception as error:
        logger.warning("slate.guard: could not inspect %s: %s", tool_name, error)
        reason = f"Run {tool_name}; Slate could not check whether it sends or spends"
    if reason is None:
        return None
    logger.info("slate.guard: approval required for %s: %s", tool_name, reason)
    digest = hashlib.sha256(f"{tool_name}:{reason}".encode()).hexdigest()[:16]
    return {"action": "approve", "message": reason, "rule_key": f"slate-guard:{digest}"}


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", guard)
