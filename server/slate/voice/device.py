import asyncio
import json
import os
import struct
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soxr
import websockets

from slate.board import ROOT
from slate.voice.audio import SAMPLE_BYTES, SAMPLE_RATE, read_wav, write_wav
from slate.voice.timing import Timeline

CLOUD = ROOT / ".local/cloud.json"
DEVICE_RATE = 16_000


@dataclass
class VoiceResult:
    transcript: str
    reply: str
    audio: bytes
    timings: dict
    commands: list[dict] = field(default_factory=list)


def cloud() -> tuple[str, str]:
    settings = json.loads(CLOUD.read_text()) if CLOUD.exists() else {}
    url = os.getenv("SLATE_CLOUD_URL") or settings.get("url")
    token = os.getenv("SLATE_DEVICE_TOKEN") or settings.get("device_token")
    if not url or not token:
        raise RuntimeError("slate.voice: no cloud settings; run make cloud-deploy")
    return url, token


def socket_url(url: str) -> str:
    if url.startswith("https://"):
        url = "wss://" + url.removeprefix("https://")
    elif url.startswith("http://"):
        url = "ws://" + url.removeprefix("http://")
    return url.rstrip("/") + "/api/device/socket"


def trim_transport_silence(pcm: bytes) -> bytes:
    first = last = None
    for index, (sample,) in enumerate(struct.iter_unpack("<h", pcm)):
        if abs(sample) > 96:
            if first is None:
                first = index
            last = index
    if first is None or last is None:
        raise RuntimeError("No audible reply arrived from Slate")
    padding = SAMPLE_RATE // 5
    return pcm[2 * max(0, first - padding) : 2 * min(len(pcm) // 2, last + padding)]


class Display:
    """The device's display state, standing in for firmware receipts."""

    def __init__(self) -> None:
        self.revision = 0
        self.color = "#4cc9f0"
        self.radius = 24.0
        self.text = ""
        self.custom = False

    def apply(self, command: dict) -> dict:
        arguments = command["arguments"]
        if command["operation"] == "set_orb":
            self.color, self.radius = arguments["color"].lower(), arguments["radius"]
            self.custom = True
            self.revision += 1
        elif command["operation"] == "show_text":
            self.text = arguments["text"]
            self.revision += 1
        return {
            "type": "receipt",
            "request_id": command["request_id"],
            "operation": command["operation"],
            "revision": self.revision,
            "state": 4,
            "color": self.color,
            "radius": float(self.radius),
            "text": self.text,
            "custom": self.custom,
        }


async def converse(
    audio: AsyncIterator[bytes],
    url: str,
    token: str,
    *,
    profile: bool = False,
) -> VoiceResult:
    timing = Timeline()
    display = Display()
    commands: list[dict] = []
    transcript = ""
    reply_audio = bytearray()
    server_timing: dict = {}
    timing.mark("connect_requested")
    async with websockets.connect(
        socket_url(url),
        additional_headers={"Authorization": f"Bearer {token}"},
        compression=None,
        max_size=None,
    ) as socket:
        timing.mark("connected")
        await socket.send(
            json.dumps({"type": "hello", "rate": DEVICE_RATE, "profile": profile})
        )
        timing.mark("turn_requested")
        await socket.send(json.dumps({"type": "start"}))
        turn = json.loads(await socket.recv())
        if turn.get("type") != "turn":
            raise RuntimeError(f"slate.voice: expected a turn, got {turn}")
        timing.mark("turn_ready")

        async def receive() -> str:
            nonlocal transcript, server_timing
            async for message in socket:
                if isinstance(message, bytes):
                    timing.mark("reply_first_frame")
                    if "reply_first_audible" not in timing.marks and any(
                        abs(sample) > 96
                        for (sample,) in struct.iter_unpack("<h", message)
                    ):
                        timing.mark("reply_first_audible")
                    reply_audio.extend(message)
                    continue
                event = json.loads(message)
                kind = event["type"]
                if kind == "command":
                    commands.append(event)
                    await socket.send(json.dumps(display.apply(event)))
                elif kind in ("error", "cancelled"):
                    raise RuntimeError(event.get("message", f"Voice turn {kind}"))
                elif kind == "transcript":
                    if event["text"].strip():
                        timing.mark("stt_first_text")
                    if event["final"]:
                        timing.mark("transcript_received")
                        transcript = event["text"]
                        if not transcript:
                            return ""
                elif kind == "reply":
                    timing.mark("reply_text_received")
                    if event["final"]:
                        timing.mark("reply_complete_received")
                        server_timing = event.get("timing", {})
                        return event["text"]
            raise RuntimeError("slate.voice: Slate closed the connection mid-turn")

        receiver = asyncio.create_task(receive())
        try:
            clock = asyncio.get_running_loop().time
            sent = 0.0
            started = clock()
            async with aclosing(aiter(audio)) as chunks:
                async for chunk in chunks:
                    if not chunk or len(chunk) % SAMPLE_BYTES:
                        raise ValueError("slate.voice: incomplete PCM samples")
                    timing.mark("input_first_capture")
                    await socket.send(chunk)
                    timing.mark("input_last_capture", replace=True)
                    sent += len(chunk) / (DEVICE_RATE * SAMPLE_BYTES)
                    await asyncio.sleep(max(0, started + sent - clock()))
            timing.mark("end_requested")
            await socket.send(json.dumps({"type": "end"}))
            reply = await asyncio.wait_for(receiver, timeout=600)
            if reply:
                await asyncio.sleep(0.5)
                timing.mark("receive_drain_done")
                if not reply_audio:
                    raise RuntimeError("Slate replied, but no reply audio arrived")
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    return VoiceResult(
        transcript,
        reply,
        write_wav(trim_transport_silence(bytes(reply_audio))) if reply else b"",
        {**server_timing, "device": timing.snapshot()},
        commands,
    )


def device_pcm(path: Path) -> bytes:
    pcm = np.frombuffer(read_wav(path.read_bytes()), dtype="<i2")
    resampled = soxr.resample(pcm, SAMPLE_RATE, DEVICE_RATE)
    padding = np.zeros(DEVICE_RATE // 5, dtype="<i2")
    return np.concatenate([padding, resampled.astype("<i2"), padding]).tobytes()


async def simulate(input_file: Path, *, profile: bool = False) -> VoiceResult:
    pcm = device_pcm(input_file)
    frame = DEVICE_RATE * SAMPLE_BYTES // 50

    async def chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(pcm), frame):
            yield pcm[offset : offset + frame]

    url, token = cloud()
    return await converse(chunks(), url, token, profile=profile)
