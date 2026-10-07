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
from slate.voice.device import simulate
from slate.voice.timing import milliseconds

CONFIG = tomllib.loads((ROOT / "experiments/agent.toml").read_text())


def summarize(timings: dict) -> dict[str, float | None]:
    dm = timings["device"]["marks_ns"]
    sm = timings["server"]["marks_ns"]
    agent = timings["agent"]
    stt = timings["stt"]["remote"]
    segments = timings["tts"]["segments"]
    first_segment = segments[0] if segments else {}
    tts = first_segment.get("remote", {})
    tts_client = first_segment.get("client", {})
    server = {
        "end": sm.get("end_requested"),
        "stt_ready": sm.get("stt_completed"),
        "agent_first_text": agent.get("first_text"),
        "tts_requested": sm.get("tts_requested"),
        "first_pcm": sm.get("tts_first_audio"),
        "first_enqueue": sm.get("reply_first_enqueue"),
    }
    result = {
        "connect_ms": milliseconds(dm, "connect_requested", "connected"),
        "turn_setup_ms": milliseconds(dm, "turn_requested", "turn_ready"),
        "input_ms": milliseconds(dm, "turn_ready", "end_requested"),
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
        "agent_first_text_to_tts_request_ms": milliseconds(
            server, "agent_first_text", "tts_requested"
        ),
        "agent_first_text_to_first_pcm_ms": milliseconds(
            server, "agent_first_text", "first_pcm"
        ),
        "tts_total_ms": milliseconds(sm, "tts_requested", "tts_completed"),
        "tts_request_to_first_pcm_ms": milliseconds(
            sm, "tts_requested", "tts_first_audio"
        ),
        "tts_first_pcm_to_enqueue_ms": milliseconds(
            sm, "tts_first_audio", "reply_first_enqueue"
        ),
        "tts_first_segment_generate_ms": tts.get("generate_ms"),
        "tts_first_segment_first_token_ms": tts.get("first_token_ms"),
        "tts_first_segment_first_pcm_ms": tts.get("first_pcm_ms"),
        "tts_first_segment_preprocess_ms": tts.get("preprocess_ms"),
        "tts_first_segment_codec_decode_ms": tts.get("codec_decode_ms"),
        "tts_first_segment_pcm_encode_cpu_ms": tts.get("pcm_encode_cpu_ms"),
        "tts_first_segment_queue_put_ms": tts.get("queue_put_ms"),
        "tts_first_segment_remote_total_ms": tts.get("remote_total_ms"),
        "tts_first_segment_client_total_ms": milliseconds(
            tts_client, "requested", "completed"
        ),
        "tts_first_segment_client_first_pcm_ms": milliseconds(
            tts_client, "requested", "first_pcm"
        ),
        "tts_first_segment_queue_setup_ms": milliseconds(
            tts_client, "requested", "queues_ready"
        ),
        "stt_model_load_ms": stt.get("model_load_ms"),
        "tts_model_load_ms": tts.get("model_load_ms"),
        "stt_model_age_ms": stt.get("model_age_ms"),
        "tts_model_age_ms": tts.get("model_age_ms"),
        "stt_queue_setup_ms": milliseconds(
            timings["stt"]["client"], "requested", "queues_ready"
        )
        if "client" in timings["stt"]
        else None,
    }
    result["end_to_stt_ms"] = milliseconds(server, "end", "stt_ready")
    result["end_to_agent_first_text_ms"] = milliseconds(
        server, "end", "agent_first_text"
    )
    result["end_to_first_pcm_ms"] = milliseconds(server, "end", "first_pcm")
    result["end_to_enqueue_ms"] = milliseconds(server, "end", "first_enqueue")
    result["end_to_audible_ms"] = milliseconds(
        dm, "end_requested", "reply_first_audible"
    )
    generated = tts.get("generate_ms")
    token = tts.get("first_token_ms")
    pcm = tts.get("first_pcm_ms")
    if (
        generated is not None
        and any(value is not None and value > generated for value in (token, pcm))
    ) or (token is not None and pcm is not None and token > pcm):
        raise ValueError("Profile contains reversed remote generation endpoints")
    if any(value is not None and value < 0 for value in result.values()):
        raise ValueError("Profile contains negative durations")
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
        "case": "cloud-voice-profile",
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
            "The device client runs on the Mac and Slate runs on Modal. "
            "end-to-audible spans the device's end message to the first non-silent "
            "reply PCM it receives, both on the device clock; it includes the "
            "network both ways but not speaker output. Server spans use the "
            "server clock and start at the server's receipt of end. Agent text, "
            "CSM generation and streaming can overlap; no inference span is "
            "subtracted. TTS remote and client metrics describe the first "
            "segment. Agent TTFT includes opaque provider queue, prefill and "
            "reasoning. Device and server timestamps are never subtracted."
        ),
    }
    directory = ROOT / CONFIG["tracking"]["raw_runs"] / metadata["profile_id"]
    directory.mkdir(parents=True)
    rows = []
    for trial in range(args.repeats):
        started = time.monotonic()
        entry = {**metadata, "trial": trial, "at": datetime.now(UTC).isoformat()}
        try:
            result = await simulate(args.input, profile=True)
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
            (directory / f"cloud-{trial}.wav").write_bytes(result.audio)
            rows.append(entry)
            print(
                f"slate.profile: trial={trial} "
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
            (directory / f"cloud-{trial}.json").write_text(json.dumps(entry, indent=2))
            append(entry)
    summary = {
        "all": aggregate(rows),
        "loaded_model_followups": aggregate(
            [
                row
                for row in rows
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
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
