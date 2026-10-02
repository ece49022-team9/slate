import argparse
import asyncio
import hashlib
import json
import statistics
import subprocess
import time
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from slate.board import ROOT
from slate.breadboard import breadboard, build_image
from slate.voice.audio import AUDIBLE_PEAK, SPEECH_PEAK, write_wav
from slate.voice.device import LiveCall
from slate.voice.firmware import RATE, listen, simulate_firmware, stereo_pcm

from scripts.profile_voice import CONFIG, append, summarize

RUNS = ROOT / ".local/agent-runs"
QUESTION = RUNS / "spoken-input.wav"
LONG_QUESTION = RUNS / "rainbow.wav"
INTERRUPTION = RUNS / "stop.wav"
QUIET_NS = 1_500_000_000
SILENCE_NS = 500_000_000
SPEAKING_NS = 300_000_000
SENTENCE_NS = 800_000_000
EXPECTED = ("12", "twelve")


def elapsed(start: int | None, end: int | None) -> float | None:
    return None if start is None or end is None else (end - start) / 1e6


def spoken(path: Path, text: str) -> Path:
    if not path.exists():
        aiff = path.with_suffix(".aiff")
        subprocess.run(["say", "-o", aiff, text], check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, path],
            check=True,
        )
        aiff.unlink()
    return path


def last_speech(mic: list, after: int, before: float) -> int | None:
    found = [ns for ns, level in mic if after <= ns < before and level > SPEECH_PEAK]
    return found[-1] if found else None


def first_speech(mic: list, after: int) -> int | None:
    return next((ns for ns, level in mic if ns >= after and level > SPEECH_PEAK), None)


def first_audible(reply: list, after: int | None) -> int | None:
    if after is None:
        return None
    frames = (ns for ns, level in reply if ns > after and level > AUDIBLE_PEAK)
    return next(frames, None)


def stopped(reply: list, after: int) -> int | None:
    """Last audible reply frame before the first half second of silence."""
    last = None
    for ns, level in reply:
        if ns < after:
            continue
        if last is not None and ns - last >= SILENCE_NS:
            return last
        if level > AUDIBLE_PEAK:
            last = ns
    return last


async def wait_started(call: LiveCall) -> None:
    async with asyncio.timeout(20):
        while not (await call.report())["session_id"]:
            await asyncio.sleep(0.2)


async def wait_speaking(call: LiveCall, after: int) -> None:
    """Wait until Slate has spoken steadily for SENTENCE_NS, which skips short
    acknowledgments such as "Checking."."""
    async with asyncio.timeout(60):
        while True:
            await asyncio.sleep(0.05)
            recent = time.monotonic_ns() - SENTENCE_NS - 200_000_000
            audible = [
                ns
                for ns, level in call.reply
                if ns > max(after, recent) and level > AUDIBLE_PEAK
            ]
            if len(audible) * 20_000_000 >= SENTENCE_NS:
                return


async def settle(call: LiveCall, after: int) -> dict:
    async with asyncio.timeout(90):
        while True:
            await asyncio.sleep(0.5)
            report = await call.report()
            pending = [
                record
                for record in report["delegations"]
                if record["created_ns"] > after
                and not first_audible(call.reply, record.get("commentary_sent_ns"))
            ]
            audible = [ns for ns, level in call.reply if level > AUDIBLE_PEAK]
            if (
                first_audible(call.reply, after)
                and not pending
                and time.monotonic_ns() - audible[-1] >= QUIET_NS
            ):
                return report


def analyze(call: LiveCall, report: dict, asked: int, interrupted: int | None) -> dict:
    speech_end = last_speech(call.mic, asked, interrupted or float("inf"))
    delegations = [
        record
        for record in report["delegations"]
        if asked < record["created_ns"] < (interrupted or float("inf"))
    ]
    first = delegations[0] if delegations else {}
    agent = first.get("agent", {})
    answer = first_audible(call.reply, first.get("commentary_sent_ns", speech_end))
    since = first.get("offset_ms", -1)
    spoken = "".join(
        part["text"]
        for part in report["words"]
        if part["role"] == "Slate" and part["start_ms"] >= since
    )
    metrics = {
        "delegated": bool(first),
        "delegations": len(delegations),
        "speech_end_to_first_audio_ms": elapsed(
            speech_end, first_audible(call.reply, speech_end)
        ),
        "speech_end_to_answer_audio_ms": elapsed(speech_end, answer),
        "speech_end_to_delegation_ms": elapsed(speech_end, first.get("created_ns")),
        "transcript_settle_ms": elapsed(
            first.get("created_ns"), first.get("transcript_ready_ns")
        ),
        "agent_total_ms": elapsed(
            first.get("agent_requested_ns"), first.get("agent_completed_ns")
        ),
        "agent_ttft_ms": elapsed(agent.get("requested"), agent.get("first_text")),
        "commentary_ack_ms": elapsed(
            first.get("commentary_sent_ns"), first.get("appended_ns")
        ),
        "commentary_to_answer_audio_ms": elapsed(
            first.get("commentary_sent_ns"), answer
        ),
    }
    result = {
        "metrics": metrics,
        "request": first.get("request"),
        "reply": first.get("reply"),
        "spoken": spoken.strip(),
        "correct": any(value in spoken.casefold() for value in EXPECTED),
        "delegation_error": first.get("error"),
    }
    if interrupted is not None:
        onset = first_speech(call.mic, interrupted)
        stop = stopped(call.reply, onset) if onset else None
        before = [
            ns
            for ns, level in call.reply
            if onset and onset - SPEAKING_NS <= ns <= onset and level > AUDIBLE_PEAK
        ]
        resumed = first_audible(call.reply, stop + SILENCE_NS) if stop else None
        metrics.update(
            speaking_at_interruption=bool(before),
            interruption_stop_ms=elapsed(onset, stop),
            resumed_after_stop_ms=elapsed(stop, resumed),
        )
        result["said_after_interruption"] = "".join(
            text for ns, text in call.said if onset and ns > onset
        ).strip()
        result["correct"] = bool(first.get("reply")) and not first.get("error")
    return result


async def live_trial(api_url: str, clip: Path, interrupt: bool, raw: Path) -> dict:
    async with breadboard(realtime=True) as bench:
        async with LiveCall(api_url, RATE) as call:

            async def stream() -> None:
                async with aclosing(listen(bench.link, "left")) as microphone:
                    async for chunk in microphone:
                        await call.send(chunk)

            streamer = asyncio.create_task(stream())
            report = None
            try:
                await wait_started(call)
                await asyncio.sleep(1)
                asked = time.monotonic_ns()
                await bench.play(stereo_pcm(clip, bench.slot))
                interrupted = None
                if interrupt:
                    await wait_speaking(call, asked)
                    interrupted = time.monotonic_ns()
                    await bench.play(stereo_pcm(INTERRUPTION, bench.slot))
                report = await settle(call, interrupted or asked)
            finally:
                streamer.cancel()
                await asyncio.gather(streamer, return_exceptions=True)
                trace = {"report": report or await call.report()}
                trace |= {"mic": call.mic, "reply": call.reply, "said": call.said}
                raw.with_suffix(".trace.json").write_text(json.dumps(trace))
                if call.reply_audio:
                    raw.with_suffix(".wav").write_bytes(
                        write_wav(bytes(call.reply_audio))
                    )
            if call.errors:
                raise RuntimeError(f"slate.profile: live session errors {call.errors}")
            return analyze(call, report, asked, interrupted) | {
                "runtime": report["agent_runtime"],
                "live_session": report["session_id"],
                "usage": report["usage"],
            }


async def cascade_trial(api_url: str, raw: Path) -> dict:
    result = await simulate_firmware(QUESTION, api_url, "left", profile=True)
    device = result.timings["device"]["marks_ns"]
    metrics = summarize(result.timings) | {
        "speech_end_to_first_audio_ms": elapsed(
            device.get("input_speech_last"), device.get("reply_first_audible")
        )
    }
    metrics["speech_end_to_answer_audio_ms"] = metrics["speech_end_to_first_audio_ms"]
    raw.with_suffix(".wav").write_bytes(result.audio)
    raw.with_suffix(".trace.json").write_text(json.dumps(result.timings))
    return {
        "metrics": metrics,
        "request": result.transcript,
        "reply": result.reply,
        "spoken": result.reply,
        "correct": any(value in result.reply.casefold() for value in EXPECTED),
        "runtime": result.timings.get("agent_runtime"),
    }


def medians(rows: list[dict]) -> dict:
    keys = {key for row in rows for key, value in row["metrics"].items()}
    summary = {}
    for key in sorted(keys):
        values = [
            row["metrics"][key]
            for row in rows
            if isinstance(row["metrics"].get(key), int | float)
            and not isinstance(row["metrics"].get(key), bool)
        ]
        if values:
            summary[key] = statistics.median(values)
    return summary


async def run(args: argparse.Namespace) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    spoken(QUESTION, "What is seven plus five?")
    spoken(LONG_QUESTION, "Can you explain how a rainbow forms?")
    spoken(INTERRUPTION, "Wait, stop.")
    sources = [
        ROOT / "server/slate/voice/live.py",
        ROOT / "server/slate/voice/live.md",
        ROOT / "server/slate/voice/device.py",
        ROOT / "server/slate/voice/session.py",
        Path(__file__),
    ]
    metadata = {
        "scope": "latency",
        "case": "duplex-voice-profile",
        "profile_id": uuid4().hex,
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "uncommitted": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
        "fixtures_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (QUESTION, LONG_QUESTION, INTERRUPTION)
        },
        "definition": (
            "Times start at the last device mic chunk with peak above "
            f"{SPEECH_PEAK} and end at the first device reply frame above "
            f"{AUDIBLE_PEAK}, both on one host's monotonic clock. Answer audio is "
            "the first audible frame after the Hermes result reached GPT-Live; "
            "first audio includes any acknowledgment. Interruption stop is the "
            "last audible reply frame before half a second of silence."
        ),
    }
    directory = ROOT / CONFIG["tracking"]["raw_runs"] / metadata["profile_id"]
    directory.mkdir(parents=True)
    await asyncio.to_thread(build_image)
    plan = []
    for trial in range(args.repeats):
        arms = [arm for arm in ("live", "cascade") if arm in args.arms]
        plan += [(arm, trial) for arm in (arms if trial % 2 == 0 else arms[::-1])]
    if "live" in args.arms:
        plan += [("interrupt", trial) for trial in range(args.interruptions)]
    rows = []
    for arm, trial in plan:
        raw = directory / f"{arm}-{trial}"
        entry = {**metadata, "arm": arm, "trial": trial}
        entry["at"] = datetime.now(UTC).isoformat()
        started = time.monotonic()
        try:
            if arm == "cascade":
                result = await cascade_trial(args.api_url, raw)
            else:
                clip = LONG_QUESTION if arm == "interrupt" else QUESTION
                result = await live_trial(args.api_url, clip, arm == "interrupt", raw)
            entry.update(status="passed", **result)
            rows.append(entry)
            shown = {
                key: round(value)
                for key, value in result["metrics"].items()
                if key.endswith("_ms") and value is not None
            }
            print(f"slate.profile: {arm} {trial} {result['correct']} {shown}")
        except Exception as error:
            entry.update(status="failed", error_type=type(error).__name__)
            entry["reason"] = str(error)
            print(f"slate.profile: {arm} {trial} failed: {error!r}")
        finally:
            entry["seconds"] = time.monotonic() - started
            raw.with_suffix(".json").write_text(json.dumps(entry, indent=2))
            append(entry)
    summary = {}
    for arm in ("live", "cascade", "interrupt"):
        group = [row for row in rows if row["arm"] == arm]
        if arm == "cascade":
            group = [row for row in group if row["trial"] > 0]
        summary[arm] = {
            "trials": len(group),
            "correct": sum(row["correct"] for row in group),
            "delegated": sum(row["metrics"].get("delegated", True) for row in group),
            "median_ms": medians(group),
        }
    denominator = {"planned": len(plan), "passed": len(rows)}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2))
    append(
        {
            **metadata,
            "case": "duplex-voice-summary",
            "status": "passed" if len(rows) == len(plan) else "partial",
            "denominator": denominator,
            "summary": summary,
        }
    )
    print(json.dumps({"denominator": denominator, **summary}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8002")
    parser.add_argument("--arms", default="live,cascade")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--interruptions", type=int, default=2)
    args = parser.parse_args()
    args.arms = args.arms.split(",")
    if args.repeats < 0 or args.interruptions < 0:
        parser.error("repeats and interruptions must not be negative")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
