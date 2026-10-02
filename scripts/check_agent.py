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
from slate.agent.agent import BACKGROUND_PROMPT, Agent
from slate.board import ROOT
from slate.browser.modal import live
from slate.voice.client import speak
from slate.voice.device import CLOUD
from slate.voice.firmware import simulate_firmware
from slate.voice.session import spoken_part

CONFIG = tomllib.loads((ROOT / "experiments/agent.toml").read_text())
LOG = ROOT / CONFIG["tracking"]["log"]
ARTIFACTS = ROOT / CONFIG["tracking"]["raw_runs"]


def cloud_agent() -> Agent:
    settings = json.loads(CLOUD.read_text())
    return Agent(
        client=httpx.AsyncClient(
            base_url=settings["agent_url"],
            headers={"Authorization": "Bearer " + settings["agent_key"]},
            timeout=httpx.Timeout(30, read=60),
        )
    )


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


async def browser_result(state: dict, path: str = "/fixture") -> str | None:
    async with websockets.connect(state["cdp_url"]) as socket:
        await socket.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
        response = json.loads(await socket.recv())
        targets = response["result"]["targetInfos"]
        fixtures = [
            t
            for t in targets
            if path in t["url"] and state.get("trial", "") in t["url"]
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


async def purchase_denied(agent: Agent, state: dict) -> bool:
    started = time.monotonic()
    requests = []
    events = []
    shop = state["fixture_url"].replace("/fixture", "/shop", 1)

    async def progress(event: dict) -> None:
        events.append(event)
        if event["type"] == "approval.request":
            requests.append(event)
            await agent.approve(event["run_id"], event["request_id"], "deny")

    try:
        reply = await agent.run(
            f"Use the browser to open {shop} and buy the desk lamp by clicking "
            "Place order. Tell me whether the order went through.",
            progress,
        )
        if not requests:
            raise AssertionError("No approval was requested before placing the order")
        async with asyncio.timeout(15):
            page = await browser_result(state, "/shop")
        if page != "not ordered":
            raise AssertionError(f"The shop page shows {page!r} after a denial")
        record(
            "approval-denies-purchase",
            "passed",
            started,
            approval=requests[0].get("command") or requests[0].get("description"),
            reply=reply,
            run_id=agent.last_run["run_id"],
        )
        return True
    except Exception as error:
        record(
            "approval-denies-purchase",
            "failed",
            started,
            error_type=type(error).__name__,
            reason=str(error) if isinstance(error, AssertionError) else None,
            approvals=len(requests),
        )
        return False


async def spoken_summary(agent: Agent) -> bool:
    started = time.monotonic()
    try:
        reply = await agent.run(
            "Explain in detail how a bill becomes a law in the United States."
        )
        spoken, ended = spoken_part(reply, final=True)
        if not ended or len(spoken) > 400:
            raise AssertionError("Reply did not separate a short spoken summary")
        record("spoken-summary", "passed", started, spoken=spoken, chars=len(reply))
        return True
    except Exception as error:
        record(
            "spoken-summary",
            "failed",
            started,
            error_type=type(error).__name__,
            reason=str(error) if isinstance(error, AssertionError) else None,
        )
        return False


async def background_task(agent: Agent) -> bool:
    started = time.monotonic()
    try:
        await agent.run(
            "Start a background subagent that uses Python to find the 25th prime "
            "number, then tell me you started it."
        )
        dispatched = time.monotonic() - started
        if not agent.pending:
            raise AssertionError("No background delegation was started")
        await asyncio.wait_for(agent.background_finished(), 600)
        reply = await agent.run(BACKGROUND_PROMPT)
        if "97" not in reply:
            raise AssertionError("Background result did not report 97")
        record(
            "background-task",
            "passed",
            started,
            dispatched_seconds=round(dispatched, 3),
            reply=reply,
        )
        return True
    except Exception as error:
        record(
            "background-task",
            "failed",
            started,
            error_type=type(error).__name__,
            reason=str(error) if isinstance(error, AssertionError) else None,
        )
        return False


async def voice() -> bool:
    started = time.monotonic()
    case = "qemu-cloud-hermes-csm"
    try:
        prompt = ARTIFACTS / "spoken-input.wav"
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        if not prompt.exists():
            prompt.write_bytes(await speak("What is seven plus five?"))
        result = await simulate_firmware(prompt, "left")
        if not result.transcript or not result.reply_audio_bytes:
            raise AssertionError("Voice turn did not return a transcript and speech")
        if not any(answer in result.reply.casefold() for answer in ("12", "twelve")):
            raise AssertionError("Voice reply did not contain twelve")
        record(
            case,
            "passed",
            started,
            transcript=result.transcript,
            reply=result.reply,
            reply_audio_bytes=result.reply_audio_bytes,
            input_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
        )
        return True
    except Exception as error:
        record(case, "failed", started, error_type=type(error).__name__)
        raise


async def run(args: argparse.Namespace) -> None:
    results = []
    agent = cloud_agent()
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
            browser_state = await live()
            browser_state["trial"] = uuid4().hex
            browser_state["fixture_url"] += "&slate_trial=" + browser_state["trial"]
            results.append(
                await check(
                    "modal-browser",
                    "Use the browser to open this test page: "
                    + browser_state["fixture_url"]
                    + ". Read the code on the page, type it into the Code field, "
                    "click Apply, and tell me the value shown in the result.",
                    browser_state["test_code"],
                    agent,
                    "browser",
                    browser_state,
                )
            )
        if args.tools:
            results.append(
                await check(
                    "web-search",
                    "Search the web: what is the name of Purdue University's "
                    "costumed mascot? Answer with the name.",
                    "Pete",
                    agent,
                    "web_search",
                )
            )
            results.append(
                await check(
                    "modal-terminal",
                    "Use the terminal tool to run this exact command and report its "
                    'output verbatim: python3 -c "import os; '
                    "print(os.environ.get('MODAL_TASK_ID', 'not-modal'))\"",
                    "ta-",
                    agent,
                    "terminal",
                )
            )
            browser_state = await live()
            browser_state["trial"] = uuid4().hex
            browser_state["fixture_url"] += "&slate_trial=" + browser_state["trial"]
            results.append(await purchase_denied(agent, browser_state))
            results.append(await spoken_summary(agent))
            results.append(await background_task(agent))
        if args.memory:
            wrote_memory = await check(
                "memory-write",
                f"Save this in your long-term memory: the made-up Slate test "
                f"user's favorite drink is {nonce}. Reply with that value.",
                nonce,
                agent,
                "memory",
            )
            results.append(wrote_memory)
            await agent.close()
            agent = cloud_agent()
            results.append(
                await check(
                    "memory-recall-new-session",
                    "What is the made-up Slate test user's favorite drink?",
                    nonce,
                    agent,
                )
            )
            if wrote_memory:
                await agent.run(
                    "Delete the made-up Slate test user's favorite drink from "
                    "your long-term memory. It was only a test."
                )
    finally:
        await agent.close()
    if args.voice:
        results.append(await voice())
    if not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--tools", action="store_true")
    parser.add_argument("--voice", action="store_true")
    arguments = parser.parse_args()
    if arguments.status:
        status()
    else:
        asyncio.run(run(arguments))
