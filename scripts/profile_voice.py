import argparse
import asyncio
import hashlib
import json
import statistics
import subprocess
import time
import tomllib
import wave
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from slate.board import ROOT
from slate.voice.firmware import simulate_firmware
from slate.voice.timing import milliseconds

CONFIG = tomllib.loads((ROOT / "experiments/agent.toml").read_text())


def summarize(timings: dict) -> dict[str, float | None]:
    device = timings["device"]
    server = timings["server"]
    if device["host"] != server["host"]:
        raise ValueError("Cross-process timing requires device and server on one host")
    dm = device["marks_ns"]
    sm = server["marks_ns"]
    agent = timings["agent"]
    simulator = timings["simulator"]["marks_ns"]
    stt = timings["stt"]["remote"]
    tts = timings["tts"]["remote"]
    result = {
        "firmware_build_ms": milliseconds(
            simulator, "build_requested", "build_completed"
        ),
        "qemu_boot_ms": milliseconds(simulator, "build_completed", "board_ready"),
        "session_setup_ms": milliseconds(dm, "session_requested", "session_ready"),
        "livekit_join_ms": milliseconds(
            dm, "livekit_connect_requested", "mic_subscribed"
        ),
        "input_and_drain_ms": milliseconds(dm, "turn_ready", "end_requested"),
        "end_rpc_ms": milliseconds(dm, "end_requested", "end_acknowledged"),
        "server_input_drain_ms": milliseconds(sm, "end_requested", "input_finished"),
        "stt_flush_ms": milliseconds(sm, "input_finished", "stt_completed"),
        "stt_first_text_ms": milliseconds(sm, "stt_requested", "stt_first_text"),
        "stt_tail_decode_ms": stt.get("tail_decode_ms"),
        "stt_tail_after_first_text_ms": stt.get("tail_decode_after_first_text_ms"),
        "stt_queue_get_ms": stt.get("queue_get_ms"),
        "stt_queue_put_ms": stt.get("queue_put_ms"),
        "stt_to_agent_ms": milliseconds(sm, "stt_completed", "agent_requested"),
        "agent_ttft_ms": milliseconds(agent, "requested", "first_text"),
        "agent_admission_ms": milliseconds(agent, "requested", "admitted"),
        "agent_text_delivery_ms": milliseconds(agent, "first_text", "last_text"),
        "agent_finalize_ms": milliseconds(agent, "last_text", "completed"),
        "agent_last_text_to_terminal_ms": milliseconds(agent, "last_text", "terminal"),
        "agent_terminal_to_completed_ms": milliseconds(agent, "terminal", "completed"),
        "agent_total_ms": milliseconds(sm, "agent_requested", "agent_completed"),
        "agent_to_tts_ms": milliseconds(sm, "agent_completed", "tts_requested"),
        "tts_total_ms": milliseconds(sm, "tts_requested", "tts_completed"),
        "tts_generate_ms": tts.get("generate_ms"),
        "tts_first_token_ms": tts.get("first_token_ms"),
        "tts_preprocess_ms": tts.get("preprocess_ms"),
        "tts_postprocess_ms": tts.get("postprocess_ms"),
        "stt_model_load_ms": stt.get("model_load_ms"),
        "tts_model_load_ms": tts.get("model_load_ms"),
        "stt_model_age_ms": stt.get("model_age_ms"),
        "tts_model_age_ms": tts.get("model_age_ms"),
        "tts_remote_total_ms": tts.get("remote_total_ms"),
        "stt_queue_setup_ms": milliseconds(
            timings["stt"]["client"], "requested", "queues_ready"
        )
        if "client" in timings["stt"]
        else None,
    }
    local = {
        "end": dm.get("end_requested"),
        "stt_ready": sm.get("stt_completed"),
        "tts_ready": sm.get("tts_completed"),
    }
    if "reply_first_audible" in dm:
        local["audible"] = dm["reply_first_audible"]
    result["end_to_stt_ms"] = milliseconds(local, "end", "stt_ready")
    result["tts_to_audible_ms"] = milliseconds(local, "tts_ready", "audible")
    result["end_to_audible_ms"] = milliseconds(local, "end", "audible")
    generate = result["tts_generate_ms"]
    first = result["tts_first_token_ms"]
    result["tts_after_first_token_ms"] = (
        generate - first if generate is not None and first is not None else None
    )
    result["tts_outside_generate_ms"] = (
        result["tts_total_ms"] - generate
        if generate is not None and result["tts_total_ms"] is not None
        else None
    )
    result["tts_transport_startup_ms"] = (
        result["tts_total_ms"] - result["tts_remote_total_ms"]
        if result["tts_total_ms"] is not None
        and result["tts_remote_total_ms"] is not None
        else None
    )
    exclusions = [
        result["agent_text_delivery_ms"],
        result["tts_after_first_token_ms"],
        result["stt_tail_after_first_text_ms"],
    ]
    observed = result["end_to_audible_ms"]
    result["retained_ttft_ms"] = (
        observed - sum(exclusions)
        if observed is not None and all(value is not None for value in exclusions)
        else None
    )
    if any(value is not None and value < 0 for value in result.values()):
        raise ValueError("Profile contains negative or overlapping duration estimates")
    return result


def append(entry: dict) -> None:
    log = ROOT / CONFIG["tracking"]["log"]
    with log.open("a") as output:
        output.write(json.dumps(entry) + "\n")


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"trials": 0, "median_ms": {}}
    return {
        "trials": len(rows),
        "median_ms": {
            key: statistics.median(
                row["metrics"][key] for row in rows if row["metrics"][key] is not None
            )
            if any(row["metrics"][key] is not None for row in rows)
            else None
            for key in rows[0]["metrics"]
        },
    }


async def run(args: argparse.Namespace) -> None:
    input_hash = hashlib.sha256(args.input.read_bytes()).hexdigest()
    with wave.open(str(args.input), "rb") as source:
        input_seconds = source.getnframes() / source.getframerate()
    sources = list((ROOT / "server/slate/voice").glob("*.py"))
    sources += list((ROOT / "server/slate/agent").glob("*.py"))
    sources += [Path(__file__), ROOT / "uv.lock"]
    metadata = {
        "scope": "latency",
        "case": "qemu-voice-profile",
        "profile_id": uuid4().hex,
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "harness_revision": CONFIG["hermes"]["revision"],
        "uncommitted": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "input_sha256": input_hash,
        "input_seconds": input_seconds,
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
        "definition": (
            "end-to-audible starts after microphone playout drains; retained_ttft "
            "subtracts observed agent first-to-last text delivery, CSM generation "
            "after first acoustic token, and STT tail decode after first text. "
            "Agent TTFT includes opaque provider queue, prefill and reasoning. "
            "Cross-host timestamps are never subtracted."
        ),
    }
    directory = ROOT / CONFIG["tracking"]["raw_runs"] / metadata["profile_id"]
    directory.mkdir(parents=True)
    rows = []
    arm = "hermes"
    for trial in range(args.repeats):
        started = time.monotonic()
        entry = {
            **metadata,
            "arm": arm,
            "trial": trial,
            "at": datetime.now(UTC).isoformat(),
        }
        try:
            result = await simulate_firmware(
                args.input, args.api_url, "left", profile=True
            )
            if not result.audio or not any(
                value in result.reply.casefold() for value in ("12", "twelve")
            ):
                raise ValueError("Spoken fixture did not return twelve and audio")
            runtime = result.timings["agent_runtime"]
            if runtime["model"] != CONFIG["hermes"]["model"]:
                raise ValueError("Profile model differs from the pinned comparison")
            entry.update(
                status="passed",
                runtime=runtime,
                seconds=time.monotonic() - started,
                timings=result.timings,
                transcript=result.transcript,
                reply=result.reply,
            )
            entry["metrics"] = summarize(result.timings)
            (directory / f"{arm}-{trial}.wav").write_bytes(result.audio)
            rows.append(entry)
            print(
                f"slate.profile: {arm} trial={trial} "
                f"end-to-audible={entry['metrics']['end_to_audible_ms']:.1f}ms "
                f"agent-ttft={entry['metrics']['agent_ttft_ms']}ms",
                flush=True,
            )
        except Exception as error:
            entry.update(
                status="failed",
                error_type=type(error).__name__,
                reason=str(error) if isinstance(error, ValueError) else None,
                seconds=time.monotonic() - started,
            )
            raise
        finally:
            (directory / f"{arm}-{trial}.json").write_text(json.dumps(entry, indent=2))
            append(entry)
    summary = {}
    for arm in ("hermes",):
        group = [entry for entry in rows if entry["arm"] == arm]
        summary[arm] = {
            "all": aggregate(group),
            "loaded_model_followups": aggregate(
                [
                    row
                    for row in group
                    if row["trial"] > 0
                    and row["metrics"]["stt_model_age_ms"] > 5000
                    and row["metrics"]["tts_model_age_ms"] > 5000
                ]
            ),
        }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2))
    append({**metadata, "status": "passed", "case": "voice-profile-summary", **summary})
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path, default=ROOT / ".local/agent-runs/spoken-input.wav"
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
