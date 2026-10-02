import argparse
import asyncio
import hashlib
import json
import subprocess
import time
import tomllib
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import websockets
from slate.agent.agent import Agent
from slate.board import ROOT
from slate.voice.client import speak
from slate.voice.firmware import simulate_firmware

CONFIG = tomllib.loads((ROOT / "experiments/agent.toml").read_text())
LOG = ROOT / CONFIG["tracking"]["log"]
ARTIFACTS = ROOT / CONFIG["tracking"]["raw_runs"]


def record(case: str, status: str, started: float, **fields) -> None:
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "case": case,
        "status": status,
        "seconds": round(time.monotonic() - started, 3),
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": hashlib.sha256(
            (ROOT / "server/slate/agent/agent.py").read_bytes()
        ).hexdigest(),
        "harness_revision": CONFIG["hermes"]["revision"],
        "uncommitted": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "scope": "smoke",
        **fields,
    }
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps(entry) + "\n")
    print(f"slate.eval: {case}: {status} ({entry['seconds']}s)", flush=True)


def status() -> None:
    latest = {}
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            entry = json.loads(line)
            latest[(entry["case"], entry.get("arm"))] = entry
    for (case, arm), entry in latest.items():
        label = case + (f" [{arm}]" if arm else "")
        score = (
            f"; {entry['correct']}/{entry['scored']} correct"
            if "correct" in entry
            else ""
        )
        print(f"{label}: {entry['status']}{score} at {entry['at']}")
    print("Smoke checks and benchmark scores are recorded separately.")


async def browser_result(state: dict) -> str | None:
    async with websockets.connect(state["cdp_url"]) as socket:
        await socket.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
        response = json.loads(await socket.recv())
        targets = response["result"]["targetInfos"]
        fixtures = [
            t
            for t in targets
            if "/fixture" in t["url"] and state.get("trial", "") in t["url"]
        ]
        fixture = fixtures[-1] if fixtures else None
        if fixture is None:
            return None
        await socket.send(
            json.dumps(
                {
                    "id": 2,
                    "method": "Target.attachToTarget",
                    "params": {"targetId": fixture["targetId"], "flatten": True},
                }
            )
        )
        while (response := json.loads(await socket.recv())).get("id") != 2:
            pass
        await socket.send(
            json.dumps(
                {
                    "id": 3,
                    "sessionId": response["result"]["sessionId"],
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": "document.getElementById('result').textContent",
                        "returnByValue": True,
                    },
                }
            )
        )
        while (response := json.loads(await socket.recv())).get("id") != 3:
            pass
        return response["result"]["result"].get("value")


async def check(
    case: str,
    message: str,
    expected: str,
    agent: Agent,
    required_tool: str | None = None,
    browser_state: dict | None = None,
) -> bool:
    started = time.monotonic()
    events = []

    async def progress(event: dict) -> None:
        events.append(event)

    try:
        reply = await agent.run(message, progress)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        artifact = ARTIFACTS / f"{agent.last_run['run_id']}.json"
        artifact.write_text(json.dumps({"run": agent.last_run, "events": events}))
        if expected.casefold() not in reply.casefold():
            raise AssertionError(f"Reply omitted expected value {expected!r}")
        if required_tool and not any(
            required_tool in json.dumps(event)
            for event in events
            if event["type"].startswith("tool.")
        ):
            raise AssertionError(f"No {required_tool} tool event was observed")
        if browser_state:
            async with asyncio.timeout(15):
                actual = await browser_result(browser_state)
            if actual != expected:
                raise AssertionError("Browser form state does not match the test code")
        record(
            case,
            "passed",
            started,
            runtime=agent.last_run["runtime"],
            usage=agent.last_run.get("usage"),
            run_id=agent.last_run["run_id"],
            artifact=str(artifact.relative_to(ROOT)),
        )
        return True
    except Exception as error:
        record(
            case,
            "failed",
            started,
            error_type=type(error).__name__,
            http_status=error.response.status_code
            if isinstance(error, httpx.HTTPStatusError)
            else None,
            reason=str(error) if isinstance(error, AssertionError) else None,
        )
        return False


async def voice(
    api_url: str = "http://127.0.0.1:8000", harness: str = "hermes"
) -> bool:
    started = time.monotonic()
    try:
        prompt = ARTIFACTS / "spoken-input.wav"
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        if not prompt.exists():
            prompt.write_bytes(await speak("What is seven plus five?"))
        result = await simulate_firmware(prompt, api_url, "left")
        reply_path = ARTIFACTS / f"spoken-reply-{harness}.wav"
        reply_path.write_bytes(result.audio)
        if not result.transcript or not result.audio:
            raise AssertionError("Voice turn did not return a transcript and audio")
        if not any(answer in result.reply.casefold() for answer in ("12", "twelve")):
            raise AssertionError("Voice reply did not contain twelve")
        record(
            f"qemu-livekit-{harness}-csm",
            "passed",
            started,
            transcript=result.transcript,
            reply=result.reply,
            reply_audio_bytes=len(result.audio),
            artifact=str(reply_path.relative_to(ROOT)),
            input_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
        )
        return True
    except Exception as error:
        record(
            f"qemu-livekit-{harness}-csm",
            "failed",
            started,
            error_type=type(error).__name__,
        )
        raise


async def run(args: argparse.Namespace) -> None:
    results = []
    agent = Agent()
    try:
        results.append(
            await check(
                "subscription-route", "Reply exactly: Slate ready", "Slate ready", agent
            )
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
            browser_state = json.loads((ROOT / ".local/browser.json").read_text())
            browser_state["trial"] = uuid4().hex
            browser_state["fixture_url"] += "&slate_trial=" + browser_state["trial"]
            results.append(
                await check(
                    "modal-browser",
                    "Use the browser to open this synthetic test page: "
                    + browser_state["fixture_url"]
                    + ". Read the code on the page, enter it into the Code field, "
                    "click Apply, and report the value shown in the result.",
                    browser_state["test_code"],
                    agent,
                    "browser",
                    browser_state,
                )
            )
        if args.memory:
            wrote_memory = await check(
                "memory-write",
                f"Remember in persistent memory: the fictitious Slate "
                f"test user's favorite beverage is {nonce}. Confirm with that value.",
                nonce,
                agent,
                "memory",
            )
            results.append(wrote_memory)
            await agent.close()
            agent = Agent()
            results.append(
                await check(
                    "memory-recall-new-session",
                    "What is the fictitious Slate test user's favorite beverage?",
                    nonce,
                    agent,
                )
            )
            if wrote_memory:
                await agent.run(
                    "Remove the fictitious Slate test user's favorite beverage "
                    "from persistent memory. It was only a test."
                )
    finally:
        await agent.close()
    if args.voice:
        results.append(await voice(args.api_url, args.harness))
    if not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--voice-only", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--harness", choices=["hermes", "openai"], default="hermes")
    arguments = parser.parse_args()
    if arguments.status:
        status()
    elif arguments.voice_only:
        asyncio.run(voice(arguments.api_url, arguments.harness))
    else:
        asyncio.run(run(arguments))
