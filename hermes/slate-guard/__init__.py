import hashlib
import logging

from tools import browser_tool, browser_tool_session

from .policy import review

logger = logging.getLogger("slate.guard")


def browser_get(key: str, query: list[str]) -> dict:
    result = browser_tool_session._run_browser_command(key, "get", query, timeout=5)
    if not result.get("success"):
        raise LookupError(result.get("error") or f"browser get {query[0]} failed")
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
    try:
        reason = review(tool_name, args or {}, lambda ref: page(task_id, ref))
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
