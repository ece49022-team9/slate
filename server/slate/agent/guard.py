import re
from collections.abc import Callable
from urllib.parse import urlsplit

SEND = re.compile(
    r"\b(send|post|publish|reply|tweet|retweet|dm|email|forward|invite|share|comment)\b"
)
SPEND = re.compile(
    r"\b(buy|purchase|checkout|check out|place (?:your )?order|order now|pay|payment|"
    r"subscribe|donate|book now|reserve|confirm (?:purchase|order|booking)|"
    r"complete (?:purchase|order)|transfer|tip|bid|add to cart and checkout)\b"
)
CHECKOUT_URL = re.compile(r"(checkout|payment|/pay\b|/cart\b|billing)")
CARD_NUMBER = re.compile(r"^(?:\d[ -]?){13,19}$")
OWN_TOOLS = ("mcp__slate_device__", "mcp_slate_device_", "kanban_")
BROWSER_ACTIONS = ("browser_click", "browser_press")


def words(name: str) -> str:
    return " ".join(part for part in re.split(r"[^a-z]+", name.lower()) if part)


def review(
    tool: str, args: dict, browser: Callable[[str | None], tuple[str, str]]
) -> str | None:
    if tool.startswith(OWN_TOOLS):
        return None
    if tool == "send_message":
        return f"Send a message to {args.get('target') or 'a contact'}"
    if tool == "browser_type":
        if CARD_NUMBER.match(str(args.get("text", "")).strip()):
            return "Enter a payment card number"
        return None
    if tool in BROWSER_ACTIONS:
        label, url = browser(args.get("ref") if tool == "browser_click" else None)
        label = " ".join(label.lower().split())
        parts = urlsplit(url)
        url = parts.netloc + parts.path
        if tool == "browser_press":
            if str(args.get("key", "")).lower() == "enter" and CHECKOUT_URL.search(
                url.lower()
            ):
                return f"Submit a checkout or payment page ({url})"
            return None
        if SPEND.search(label):
            return f'Click "{label[:60]}" on {url}, which may spend money'
        if SEND.search(label):
            return f'Click "{label[:60]}" on {url}, which may send a message'
        return None
    name = words(args.get("name", "") if tool == "tool_call" else tool)
    if SPEND.search(name):
        return f"Run {name}, which may spend money"
    if SEND.search(name):
        return f"Run {name}, which may send a message"
    return None
