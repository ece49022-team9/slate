import argparse
import asyncio
import hashlib
import json
import subprocess
import time
import tomllib
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from openai import __version__ as OPENAI_VERSION
from slate.agent.managed import ManagedAgent
from slate.board import ROOT

CONFIG = tomllib.loads((ROOT / "experiments/agent.toml").read_text())
LOG = ROOT / CONFIG["tracking"]["log"]
ARTIFACTS = ROOT / CONFIG["tracking"]["raw_runs"]


async def check(
    case: str,
    message: str,
    expected: str,
    agent: ManagedAgent,
    *,
    browser: bool = False,
    approval_origin: str | None = None,
    redact_values: tuple[str, ...] = (),
) -> bool:
    started = time.monotonic()
    events = []
    status = "failed"
    error_type = None
    reason = None
    reply = None

    async def progress(event: dict) -> None:
        events.append(event)
        if event["type"] == "approval.request":
            request = event["request"]
            choice = (
                "once"
                if browser
                and request["type"] == "browser_origin_access"
                and request["origin"] == approval_origin
                else "deny"
            )
            await agent.approve(event["run_id"], event["request_id"], choice)

    try:
        reply = await agent.run(message, progress)
        if expected.casefold() not in reply.casefold():
            raise AssertionError(f"Reply omitted expected value {expected!r}")
        if browser and not any(
            event["type"] == "agent.session.turn.item.done"
            and event["item"]["type"] == "computer_use_call"
            and event["item"]["status"] == "completed"
            for event in events
        ):
            raise AssertionError("No completed managed computer-use call was observed")
        if browser and not any(
            event["type"] == "agent.session.turn.item.done"
            and event["item"]["type"] == "computer_use_call"
            and event["item"].get("output", {}).get("type") == "computer_screenshot"
            for event in events
            if event.get("item", {}).get("output") is not None
        ):
            raise AssertionError("No managed browser screenshot was observed")
        status = "passed"
    except Exception as error:
        error_type = type(error).__name__
        if isinstance(error, AssertionError):
            reason = str(error)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    artifact = ARTIFACTS / f"managed-{case}-{uuid4().hex}.json"
    raw = json.dumps(
        {
            "input": message,
            "expected": expected,
            "requires_computer_use": browser,
            "run": agent.last_run,
            "events": events,
            "reply": reply,
        }
    )
    for value in redact_values:
        raw = raw.replace(value, "[redacted]")
    artifact.write_text(raw)
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "case": "managed-" + case,
        "status": status,
        "seconds": round(time.monotonic() - started, 3),
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "uncommitted": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "source_sha256": hashlib.sha256(
            (ROOT / "server/slate/agent/managed.py").read_bytes()
        ).hexdigest(),
        "provider": agent.provider,
        "harness": "OpenAI Agents API",
        "sdk_version": OPENAI_VERSION,
        "computer_use_enabled": agent.browser,
        "independent_browser_state_verified": False if browser else None,
        "evidence": "unpredictable fixture code, computer-use calls, screenshots"
        if browser
        else "completed SDK final result",
        "model": agent.model,
        "runtime": agent.last_run.get("runtime"),
        "run_id": agent.last_run.get("run_id"),
        "session_id": agent.last_run.get("session_id"),
        "usage": agent.last_run.get("usage"),
        "scope": "smoke",
        "artifact": str(artifact.relative_to(ROOT)),
        "error_type": error_type,
        "reason": reason,
    }
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps(entry) + "\n")
    print(f"slate.eval: {entry['case']}: {status}", flush=True)
    return status == "passed"


async def run(args: argparse.Namespace) -> None:
    agent = ManagedAgent(model=CONFIG["hermes"]["model"], browser=args.browser)
    results = []
    try:
        results.append(
            await check("route", "Reply exactly: Slate ready", "Slate ready", agent)
        )
        nonce = "slate-" + uuid4().hex[:12]
        results.append(
            await check(
                "session-write", f"The test word is {nonce}. Repeat it.", nonce, agent
            )
        )
        results.append(
            await check("session-recall", "What was the test word?", nonce, agent)
        )
        if args.browser:
            fixture = json.loads((ROOT / ".local/browser.json").read_text())
            fixture_url = fixture["fixture_url"] + "&slate_trial=" + uuid4().hex
            parsed = urlsplit(fixture["fixture_url"])
            origin = f"{parsed.scheme}://{parsed.netloc}"
            tokens = tuple(parse_qs(parsed.query).get("_modal_connect_token", []))
            results.append(
                await check(
                    "browser",
                    "Use the browser to open this test page: "
                    + fixture_url
                    + ". Read the code on the page, type it into the Code field, "
                    "click Apply, and tell me the value shown in the result.",
                    fixture["test_code"],
                    agent,
                    browser=True,
                    approval_origin=origin,
                    redact_values=tokens,
                )
            )
            if results[-1]:
                results.append(
                    await check(
                        "browser-readback",
                        "Use the browser to look at the test page that is "
                        "already open. Read the text in the element with id "
                        "result. Take a new screenshot after the page updates. "
                        "Tell me that text, and don't change the form again.",
                        fixture["test_code"],
                        agent,
                        browser=True,
                        approval_origin=origin,
                        redact_values=tokens,
                    )
                )
    finally:
        await agent.close()
    if not all(results):
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
