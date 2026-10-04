import argparse
import asyncio
import hashlib
import itertools
import json
import os
import statistics
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import modal
from slate.agent import Agent
from slate.board import ROOT
from slate.breadboard import Breadboard, breadboard, build_image
from slate.voice.device import cloud
from slate.voice.firmware import stereo_pcm

from scripts.profile_live import (
    Device,
    Service,
    end_call,
    guest_url,
    settle,
    spoken,
    start_call,
)
from scripts.profile_voice import CONFIG, append

CLIPS = ROOT / ".local/agent-runs/conversation"
TURNS = [
    "Hi Slate, how's your day going?",
    "Please remember that my friend Alex Testperson's birthday is March fourteenth.",
    "What is seven plus five?",
    "Now multiply that by three.",
    "Can you explain how a rainbow forms?",
    "Why is the sky blue?",
    "Give me three ideas for a quick weeknight dinner.",
    "Which of those is the fastest to make?",
    "Plan a ten minute morning stretch routine for me.",
    "Make it five minutes instead.",
    "What's the capital of Australia?",
    "About how far is that from Sydney?",
    "When is Alex Testperson's birthday?",
    "Summarize what we've talked about so far.",
    "Thanks, that's all for now.",
]
THINK_S = 2.0
SCALEDOWN_S = 60
GPT_LIVE_PER_MINUTE = 0.05
TOKEN_PRICES = {
    "gpt-6.1-sol": {"input": 2.0, "cached": 0.1, "write": 2.5, "output": 10.0}
}
PRICE_SOURCES = {
    "gpt-live-1": "https://developers.openai.com/api/docs/models/gpt-live-1.md",
    "gpt-6.1-sol": "https://developers.openai.com/api/docs/models/gpt-6.1-sol.md",
    "modal": "modal.Workspace.billing.rates()",
}
GPU_RATES = {"NVIDIA A10": "gpu_hour_cost_a10g", "NVIDIA L4": "gpu_hour_cost_l4"}
GPU_RATES["NVIDIA L40S"] = "gpu_hour_cost_l40s"


def gpu_per_second(name: str, rates: dict) -> float:
    return float(rates[GPU_RATES[name]]) / 3600


def token_cost(usage: dict | None, model: str) -> float:
    if not usage:
        return 0.0
    price = TOKEN_PRICES[model]
    cached = usage.get("cache_read_tokens", 0)
    written = usage.get("cache_write_tokens", 0)
    fresh = max(usage.get("input_tokens", 0) - cached - written, 0)
    return (
        fresh * price["input"]
        + cached * price["cached"]
        + written * price["write"]
        + usage.get("output_tokens", 0) * price["output"]
    ) / 1e6


def seconds(start: int, end: int) -> float:
    return (end - start) / 1e9


def growth(points: list[tuple[int, int]]) -> float:
    """Median slope across every pair of turns, so one tool turn with an extra
    model call does not decide the trend."""
    slopes = [
        (later - earlier) / (end - start)
        for (start, earlier), (end, later) in itertools.combinations(points, 2)
        if end != start
    ]
    return statistics.median(slopes) if slopes else 0.0


async def talk(bench: Breadboard, clip: Path) -> tuple[str, str]:
    """One push-to-talk turn on the firmware: hold Listen, play the clip,
    release, and wait for the reply audio to finish."""
    heard = asyncio.ensure_future(bench.link.wait_for("slate.transcript:", 180))
    said = asyncio.ensure_future(bench.link.wait_for("slate.reply:", 300))
    played = asyncio.ensure_future(bench.link.wait_for("slate.reply.audio:", 360))
    failed = asyncio.ensure_future(bench.link.wait_for("slate.cloud.error:", 360))
    try:
        bench.link.type("1")
        await bench.link.wait_for("slate.state: 1")
        await bench.play(stereo_pcm(clip, bench.slot))
        bench.link.type("3")
        await asyncio.wait({played, failed}, return_when=asyncio.FIRST_COMPLETED)
        if failed.done():
            raise RuntimeError(failed.result())
        return heard.result().split(": ", 1)[1], said.result().split(": ", 1)[1]
    finally:
        for waiter in (heard, said, played, failed):
            waiter.cancel()
        await asyncio.gather(heard, said, played, failed, return_exceptions=True)


async def cascade(service: Service, clips: list[Path], model: str, rates: dict):
    turns = []
    async with breadboard(realtime=True) as bench:
        await bench.link.wait_for("slate.cloud: connected", 60)
        async with Device(bench, 0):
            try:
                await talk(bench, clips[0])
            except RuntimeError as error:
                print(f"slate.cost: warm-up reply failed but loaded models: {error}")
            known = set(await service.reports())
            began = time.monotonic_ns()
            for index, clip in enumerate(clips):
                started = time.monotonic_ns()
                try:
                    heard, said = await talk(bench, clip)
                    turns.append({"turn": index, "heard": heard, "reply": said})
                except RuntimeError as error:
                    turns.append({"turn": index, "failed": str(error)})
                    print(f"slate.cost: cascade turn {index} failed: {error}")
                turns[-1]["seconds"] = seconds(started, time.monotonic_ns())
                await asyncio.sleep(THINK_S - 0.5)
            ended = time.monotonic_ns()
            reports = [
                report
                for key, report in (await service.reports()).items()
                if key not in known and "transcript" in report
            ]
    answered = [turn for turn in turns if "failed" not in turn]
    if len(reports) != len(answered):
        raise RuntimeError(
            f"slate.cost: {len(answered)} answered turns but {len(reports)} reports"
        )
    for turn, report in zip(answered, reports, strict=True):
        timing = report["timing"]
        if not timing["stt"]:
            raise RuntimeError("slate.cost: run the service with SLATE_PROFILE=1")
        turn["usage"] = timing.get("agent_usage")
        turn["stt"] = timing["stt"]["client"] | {"gpu": timing["stt"]["remote"]["gpu"]}
        turn["tts"] = [
            segment["client"] | {"gpu": segment["remote"]["gpu"]}
            for segment in timing["tts"]["segments"]
        ]
    for turn in turns:
        turn["agent_usd"] = token_cost(turn.get("usage"), model)
    apps = {}
    for app in ("stt", "tts"):
        calls = [
            call
            for turn in answered
            for call in (turn[app] if app == "tts" else [turn[app]])
        ]
        gpu = Counter(call["gpu"] for call in calls).most_common(1)[0][0]
        first = min(call["requested"] for call in calls)
        window = seconds(first, ended) + SCALEDOWN_S
        busy = sum(seconds(call["requested"], call["completed"]) for call in calls)
        apps[app] = {
            "gpu": gpu,
            "container_seconds": window,
            "call_seconds": busy,
            "usd": window * gpu_per_second(gpu, rates),
        }
    agent_usd = sum(turn["agent_usd"] for turn in turns)
    modal_usd = sum(app["usd"] for app in apps.values())
    return {
        "turns": turns,
        "failed_turns": len(turns) - len(answered),
        "conversation_seconds": seconds(began, ended),
        "modal": apps,
        "agent_usd": agent_usd,
        "modal_usd": modal_usd,
        "total_usd": agent_usd + modal_usd,
    }


async def live(service: Service, clips: list[Path], model: str) -> dict:
    turns = []
    async with breadboard(realtime=True) as bench:
        await bench.link.wait_for("slate.cloud: connected", 60)
        async with Device(bench, 0) as device:
            call_id = await start_call(bench)
            try:
                await asyncio.sleep(1)
                began = time.monotonic_ns()
                for index, clip in enumerate(clips):
                    started = time.monotonic_ns()
                    await bench.play(stereo_pcm(clip, bench.slot))
                    await settle(device, service, call_id, started)
                    turns.append(
                        {
                            "turn": index,
                            "started_ns": started,
                            "seconds": seconds(started, time.monotonic_ns()),
                        }
                    )
                    print(f"slate.cost: live turn {index}")
                    await asyncio.sleep(THINK_S - 1.5)
                ended = time.monotonic_ns()
            finally:
                await end_call(bench)
            report = await service.report(call_id)
    for turn, following in zip(turns, turns[1:] + [None], strict=True):
        until = following["started_ns"] if following else float("inf")
        handed = [
            record
            for record in report["delegations"]
            if turn["started_ns"] < record["created_ns"] < until
        ]
        turn["delegations"] = len(handed)
        turn["usage"] = [record.get("usage") for record in handed]
        turn["agent_usd"] = sum(
            token_cost(record.get("usage"), model) for record in handed
        )
    billed = report["seconds"]
    agent_usd = sum(turn["agent_usd"] for turn in turns)
    live_usd = billed / 60 * GPT_LIVE_PER_MINUTE
    return {
        "turns": turns,
        "conversation_seconds": seconds(began, ended),
        "billed_seconds": billed,
        "live_usd": live_usd,
        "agent_usd": agent_usd,
        "total_usd": agent_usd + live_usd,
        "slate_said": " ".join(
            part["text"] for part in report["words"] if part["role"] == "Slate"
        ).strip(),
    }


def runs(turn: dict) -> list[dict]:
    usage = turn.get("usage")
    if isinstance(usage, dict):
        return [usage]
    return [item for item in usage or [] if item]


def project(result: dict, arm: str, rates: dict, model: str) -> dict:
    """Hermes resends its prompt and history every run, so input tokens grow by a
    fixed amount per turn. Price that growth at the observed blended input price,
    which already includes cache hits, misses, and tool calls."""
    turns = result["turns"]
    per_turn_s = result["conversation_seconds"] / len(turns)
    usage = [(turn["turn"], item) for turn in turns for item in runs(turn)]
    slope = growth([(index, item["input_tokens"]) for index, item in usage])
    input_tokens = sum(item["input_tokens"] for _, item in usage)
    input_usd = sum(token_cost(item | {"output_tokens": 0}, model) for _, item in usage)
    input_price = input_usd / input_tokens
    mean_run = result["agent_usd"] / len(usage)
    mean_turn = statistics.fmean(index for index, _ in usage)
    runs_per_turn = len(usage) / len(turns)
    rows = {}
    for count in (len(turns), 50, 100, 200):
        minutes = count * per_turn_s / 60
        agent = runs_per_turn * sum(
            mean_run + slope * (index - mean_turn) * input_price
            for index in range(count)
        )
        if arm == "cascade":
            voice = sum(
                (count * per_turn_s + SCALEDOWN_S) * gpu_per_second(app["gpu"], rates)
                for app in result["modal"].values()
            )
        else:
            voice = minutes * GPT_LIVE_PER_MINUTE
        rows[count] = {
            "minutes": minutes,
            "voice_usd": voice,
            "agent_usd": agent,
            "total_usd": voice + agent,
        }
    if arm == "cascade":
        apps = result["modal"].values()
        idle_hour = sum(SCALEDOWN_S * gpu_per_second(app["gpu"], rates) for app in apps)
    else:
        idle_hour = 60 * GPT_LIVE_PER_MINUTE
    return {
        "seconds_per_turn": per_turn_s,
        "agent_runs_per_turn": runs_per_turn,
        "agent_usd_per_run": mean_run,
        "input_tokens_per_run": input_tokens / len(usage),
        "input_token_growth_per_turn": slope,
        "blended_input_usd_per_million": input_price * 1e6,
        "idle_hour_usd": idle_hour,
        "turns": rows,
    }


async def modal_bill(since: datetime, environment: str) -> dict:
    """Everything billed to the speech models' Modal environment since the hour
    started."""
    hour = since.replace(minute=0, second=0, microsecond=0)
    items = await modal.Workspace.from_context().billing.report.aio(
        start=hour, resolution="h"
    )
    bill: dict[str, dict[str, float]] = {}
    for item in items:
        if item.environment_name != environment:
            continue
        app = bill.setdefault(item.description, {})
        for resource, cost in item.cost_by_resource.items():
            app[resource] = app.get(resource, 0.0) + float(cost)
    return bill


async def run(args: argparse.Namespace) -> None:
    CLIPS.mkdir(parents=True, exist_ok=True)
    clips = [
        spoken(CLIPS / f"{index:02d}.wav", text) for index, text in enumerate(TURNS)
    ]
    model = CONFIG["hermes"]["model"]
    rates = dict((await modal.Workspace.from_context().billing.rates.aio()).items())
    api_url, token = cloud()
    if args.api_url:
        api_url = args.api_url
        os.environ["SLATE_CLOUD_URL"] = guest_url(api_url)
    service = Service(api_url, token)
    started_at = datetime.now(UTC)
    metadata = {
        "scope": "cost",
        "case": "conversation-cost",
        "profile_id": uuid4().hex,
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "turns": TURNS,
        "think_seconds": THINK_S,
        "model": model,
        "prices": {
            "gpt_live_per_minute": GPT_LIVE_PER_MINUTE,
            "tokens_per_million": TOKEN_PRICES[model],
            "modal_gpu_hour": {
                name: float(rates[key]) for name, key in GPU_RATES.items()
            },
            "checked": started_at.date().isoformat(),
            "sources": PRICE_SOURCES,
        },
        "clips_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in clips
        },
        "definition": (
            "One scripted conversation per arm through the firmware simulator, "
            f"{THINK_S:.0f}s of user silence after Slate stops. Agent cost is "
            "Hermes's reported tokens at API prices (the subscription pays in "
            "practice). Cascade voice cost is GPU time while Kyutai and CSM stay "
            f"warm, plus the {SCALEDOWN_S}s scale-down tail, excluding Modal CPU "
            "and memory. GPT-Live voice cost is its billed session seconds. Both "
            "arms run the firmware in QEMU against one Slate service over the "
            "device WebSocket."
        ),
    }
    directory = ROOT / CONFIG["tracking"]["raw_runs"] / metadata["profile_id"]
    directory.mkdir(parents=True)
    await asyncio.to_thread(build_image)
    results = {}
    try:
        if "cascade" in args.arms:
            results["cascade"] = await cascade(service, clips, model, rates)
        if "live" in args.arms:
            results["live"] = await live(service, clips, model)
    finally:
        cleanup = Agent()
        try:
            await cleanup.run(
                "Delete what you saved about Alex Testperson's birthday. "
                "It was only a test."
            )
        finally:
            await cleanup.close()
    for arm, result in results.items():
        result["projection"] = project(result, arm, rates, model)
        (directory / f"{arm}.json").write_text(json.dumps(result, indent=2))
    entry = {
        **metadata,
        "at": datetime.now(UTC).isoformat(),
        "status": "passed" if len(results) == len(args.arms) else "partial",
        "results": {
            arm: {
                key: value
                for key, value in result.items()
                if key not in ("turns", "slate_said")
            }
            for arm, result in results.items()
        },
        "modal_bill_since_start": await modal_bill(
            started_at - timedelta(minutes=5), args.modal_environment
        ),
    }
    append(entry)
    print(json.dumps(entry["results"], indent=2))
    print("modal bill so far:", entry["modal_bill_since_start"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", help="defaults to the deployed service")
    parser.add_argument("--modal-environment", default="main")
    parser.add_argument("--arms", default="cascade,live")
    args = parser.parse_args()
    args.arms = args.arms.split(",")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
