import argparse
import asyncio
import hashlib
import json
import os
import statistics
import subprocess
import time
from array import array
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from slate.board import ROOT
from slate.breadboard import Breadboard, breadboard, build_image
from slate.voice.audio import write_wav
from slate.voice.device import cloud
from slate.voice.firmware import stereo_pcm
from slate.voice.live import Words, lines
from slate.voice.sim import Echo

from scripts.profile_voice import CONFIG, append

RUNS = ROOT / ".local/agent-runs"
QUESTION = RUNS / "spoken-input.wav"
LONG_QUESTION = RUNS / "rainbow.wav"
INTERRUPTION = RUNS / "stop.wav"
SPEECH_PEAK = 300
AUDIBLE_PEAK = 96
FRAME_NS = 20_000_000
QUIET_NS = 1_500_000_000
SILENCE_NS = 500_000_000
SPEAKING_NS = 300_000_000
SENTENCE_NS = 800_000_000
STOPPED_MS = 2000
EXPECTED = ("12", "twelve")


def peak(pcm: bytes) -> int:
    return max(map(abs, array("h", pcm)), default=0)


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


def guest_url(url: str) -> str:
    """The firmware in QEMU reaches the Mac at 10.0.2.2."""
    parts = urlsplit(url)
    if parts.hostname not in ("127.0.0.1", "localhost"):
        return url
    return urlunsplit(parts._replace(netloc=f"10.0.2.2:{parts.port or 80}"))


class Device:
    """What the simulated board heard and played, on the host's monotonic clock:
    the firmware's microphone frames and the reply audio it received, both
    copied to serial by the audio tap."""

    def __init__(self, bench: Breadboard, echo: float) -> None:
        self.bench = bench
        self.mic: list[tuple[int, int]] = []
        self.reply: list[tuple[int, int]] = []
        self.audio = bytearray()
        self.echo = Echo(bench, echo) if echo else None
        self.tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> "Device":
        self.tasks = [
            asyncio.create_task(self.listen()),
            asyncio.create_task(self.play()),
        ]
        self.bench.link.type("a")
        return self

    async def __aexit__(self, *exc) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def listen(self) -> None:
        while True:
            pcm = await self.bench.link.audio.get()
            self.mic.append((time.monotonic_ns(), peak(pcm)))

    async def play(self) -> None:
        while True:
            pcm = await self.bench.link.speaker.get()
            self.reply.append((time.monotonic_ns(), peak(pcm)))
            self.audio += pcm
            if self.echo:
                self.echo.push(pcm)

    def last_speech(self, after: int, before: int) -> int | None:
        found = [
            ns for ns, level in self.mic if after <= ns < before and level > SPEECH_PEAK
        ]
        return found[-1] if found else None

    def first_speech(self, after: int) -> int | None:
        loud = (ns for ns, level in self.mic if ns >= after and level > SPEECH_PEAK)
        return next(loud, None)

    def first_audible(self, after: int | None) -> int | None:
        if after is None:
            return None
        loud = (ns for ns, level in self.reply if ns > after and level > AUDIBLE_PEAK)
        return next(loud, None)

    def audible(self, after: int) -> list[int]:
        return [ns for ns, level in self.reply if ns > after and level > AUDIBLE_PEAK]

    def stopped(self, after: int) -> int | None:
        """Last audible reply frame before the first half second of silence."""
        last = None
        for ns, level in self.reply:
            if ns < after:
                continue
            if last is not None and ns - last >= SILENCE_NS:
                return last
            if level > AUDIBLE_PEAK:
                last = ns
        return last

    async def speaking(self, after: int) -> None:
        """Waits until Slate has spoken steadily for SENTENCE_NS, which skips
        short acknowledgments such as "Checking."."""
        async with asyncio.timeout(120):
            while True:
                await asyncio.sleep(0.05)
                recent = time.monotonic_ns() - SENTENCE_NS - 200_000_000
                if len(self.audible(max(after, recent))) * FRAME_NS >= SENTENCE_NS:
                    return


class Service:
    def __init__(self, api_url: str, token: str) -> None:
        self.client = httpx.AsyncClient(
            base_url=api_url, headers={"Authorization": f"Bearer {token}"}
        )

    async def reports(self) -> dict[str, dict]:
        response = await self.client.get("/api/voice/reports")
        response.raise_for_status()
        return response.json()

    async def report(self, report_id: str) -> dict:
        found = (await self.reports()).get(report_id)
        if found is None:
            raise RuntimeError(f"slate.profile: the service has no report {report_id}")
        return found


async def start_call(bench: Breadboard) -> str:
    started = asyncio.ensure_future(bench.link.wait_for("slate.live: started", 30))
    bench.link.type("c")
    return (await started).removeprefix("slate.live: started ").strip()


async def end_call(bench: Breadboard) -> None:
    ended = asyncio.ensure_future(bench.link.wait_for("slate.live: ended", 30))
    bench.link.type("c")
    await ended


async def settle(device: Device, service: Service, call_id: str, after: int) -> dict:
    """Waits until every handoff since `after` has been answered out loud and
    Slate has been quiet for QUIET_NS."""
    async with asyncio.timeout(180):
        while True:
            await asyncio.sleep(0.5)
            report = await service.report(call_id)
            pending = [
                record
                for record in report["delegations"]
                if record["created_ns"] > after
                and not device.first_audible(record.get("commentary_sent_ns", 2**63))
            ]
            heard = device.audible(after)
            if heard and not pending and time.monotonic_ns() - heard[-1] >= QUIET_NS:
                return report


def slate_words(report: dict, after_ms: int) -> str:
    said = [part for part in report["words"] if part["start_ms"] >= after_ms]
    return " ".join(text for role, text in lines_of(said) if role == "Slate")


def lines_of(words: list[dict]) -> list[tuple[str, str]]:
    return lines([Words(**part) for part in words])


def analyze(
    device: Device,
    report: dict,
    asked: int,
    played: int,
    interrupted: int | None,
    onset: int | None,
) -> dict:
    speech_end = device.last_speech(asked, played)
    handoffs = [
        record
        for record in report["delegations"]
        if asked < record["created_ns"] < (interrupted or 2**63)
    ]
    first = handoffs[0] if handoffs else {}
    agent = first.get("agent", {})
    answer = device.first_audible(first.get("commentary_sent_ns", speech_end))
    said = slate_words(report, first.get("offset_ms", 0))
    conversation = lines_of(report["words"])
    metrics = {
        "delegated": bool(first),
        "delegations": len(handoffs),
        "speech_end_to_first_audio_ms": elapsed(
            speech_end, device.first_audible(speech_end)
        ),
        "speech_end_to_answer_audio_ms": elapsed(speech_end, answer),
        "speech_end_to_delegation_ms": elapsed(speech_end, first.get("created_ns")),
        "agent_total_ms": elapsed(
            first.get("agent_requested_ns"), first.get("agent_completed_ns")
        ),
        "agent_ttft_ms": elapsed(agent.get("requested"), agent.get("first_text")),
        "commentary_to_answer_audio_ms": elapsed(
            first.get("commentary_sent_ns"), answer
        ),
        "user_lines": sum(role == "User" for role, _ in conversation),
        "expected_user_lines": 1 if interrupted is None else 2,
    }
    result = {
        "metrics": metrics,
        "request": first.get("request"),
        "reply": first.get("reply"),
        "spoken": said,
        "correct": any(value in said.casefold() for value in EXPECTED),
        "delegation_error": first.get("error"),
        "runtime": first.get("runtime"),
        "seconds_billed": report["seconds"],
    }
    if interrupted is not None:
        onset = onset or device.first_speech(interrupted)
        stop = device.stopped(onset) if onset else None
        before = [
            ns
            for ns in device.audible(onset - SPEAKING_NS if onset else 2**63)
            if ns <= onset
        ]
        metrics.update(
            speaking_at_interruption=bool(before),
            interruption_stop_ms=elapsed(onset, stop),
            resumed_after_stop_ms=elapsed(
                stop, device.first_audible(stop + SILENCE_NS) if stop else None
            ),
        )
        users = [part for part in report["words"] if part["role"] == "User"]
        result["said_after_interruption"] = slate_words(
            report, max(part["end_ms"] for part in users) if users else 0
        )
        result["correct"] = bool(
            before and metrics["interruption_stop_ms"] is not None
        ) and (metrics["interruption_stop_ms"] < STOPPED_MS)
    return result


def speech_offset_ns(clip: Path, slot: int) -> int:
    """When speech starts inside the padded clip the breadboard plays."""
    stereo = array("h", stereo_pcm(clip, slot))
    loud = next(i for i, sample in enumerate(stereo) if abs(sample) > SPEECH_PEAK)
    return (loud // 2) * 1_000_000_000 // 16_000


async def live_trial(
    service: Service, clip: Path, interrupt: bool, raw: Path, echo: float
) -> dict:
    async with breadboard(realtime=True) as bench:
        await bench.link.wait_for("slate.cloud: connected", 60)
        async with Device(bench, echo) as device:
            call_id = await start_call(bench)
            report = None
            try:
                await asyncio.sleep(1)
                asked = time.monotonic_ns()
                await bench.play(stereo_pcm(clip, bench.slot))
                played = time.monotonic_ns()
                interrupted = onset = None
                if interrupt:
                    await device.speaking(asked)
                    interrupted = time.monotonic_ns()
                    if echo:
                        onset = interrupted + speech_offset_ns(INTERRUPTION, bench.slot)
                    await bench.play(stereo_pcm(INTERRUPTION, bench.slot))
                report = await settle(device, service, call_id, interrupted or asked)
            finally:
                await end_call(bench)
                report = await service.report(call_id)
                trace = {"report": report, "mic": device.mic, "reply": device.reply}
                raw.with_suffix(".trace.json").write_text(json.dumps(trace))
                if device.audio:
                    raw.with_suffix(".wav").write_bytes(write_wav(bytes(device.audio)))
            return analyze(device, report, asked, played, interrupted, onset) | {
                "echo": echo,
                "interruption_onset": "clip schedule" if onset else "mic level",
            }


async def cascade_trial(raw: Path) -> dict:
    async with breadboard(realtime=True) as bench:
        await bench.link.wait_for("slate.cloud: connected", 60)
        async with Device(bench, 0) as device:
            heard = asyncio.ensure_future(bench.link.wait_for("slate.transcript:", 180))
            said = asyncio.ensure_future(bench.link.wait_for("slate.reply:", 300))
            done = asyncio.ensure_future(bench.link.wait_for("slate.reply.audio:", 360))
            bench.link.type("1")
            await bench.link.wait_for("slate.state: 1")
            asked = time.monotonic_ns()
            await bench.play(stereo_pcm(QUESTION, bench.slot))
            played = time.monotonic_ns()
            bench.link.type("3")
            transcript = (await heard).split(": ", 1)[1]
            reply = (await said).split(": ", 1)[1]
            await done
            if device.audio:
                raw.with_suffix(".wav").write_bytes(write_wav(bytes(device.audio)))
            speech_end = device.last_speech(asked, played)
            first = elapsed(speech_end, device.first_audible(speech_end))
    return {
        "metrics": {
            "speech_end_to_first_audio_ms": first,
            "speech_end_to_answer_audio_ms": first,
        },
        "request": transcript,
        "reply": reply,
        "spoken": reply,
        "correct": any(value in reply.casefold() for value in EXPECTED),
    }


def medians(rows: list[dict]) -> dict:
    keys = {key for row in rows for key in row["metrics"]}
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
    api_url, token = cloud()
    if args.api_url:
        api_url = args.api_url
        os.environ["SLATE_CLOUD_URL"] = guest_url(api_url)
    service = Service(api_url, token)
    sources = [
        ROOT / "server/slate/voice/live.py",
        ROOT / "server/slate/voice/live.md",
        ROOT / "server/slate/voice/session.py",
        ROOT / "firmware/main/cloud.cpp",
        Path(__file__),
    ]
    metadata = {
        "scope": "latency",
        "case": "duplex-voice-profile",
        "transport": "device websocket",
        "api_url": api_url,
        "echo": args.echo,
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
            "Times start at the last firmware mic frame with peak above "
            f"{SPEECH_PEAK} and end at the first reply frame the firmware received "
            f"with peak above {AUDIBLE_PEAK}, both copied to serial by the audio "
            "tap and stamped on one host's monotonic clock. Answer audio is the "
            "first audible frame after the Hermes result reached GPT-Live; first "
            "audio includes any acknowledgment. Interruption stop is the last "
            "audible reply frame before half a second of silence; an interruption "
            f"trial is correct when Slate was speaking and stopped within {STOPPED_MS} "
            "ms."
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
                result = await cascade_trial(raw)
            else:
                clip = LONG_QUESTION if arm == "interrupt" else QUESTION
                result = await live_trial(
                    service, clip, arm == "interrupt", raw, args.echo
                )
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
    parser.add_argument("--api-url", help="defaults to the deployed service")
    parser.add_argument("--arms", default="live,cascade")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--interruptions", type=int, default=2)
    parser.add_argument("--echo", type=float, default=0.0)
    args = parser.parse_args()
    args.arms = args.arms.split(",")
    if args.repeats < 0 or args.interruptions < 0:
        parser.error("repeats and interruptions must not be negative")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
