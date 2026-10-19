import asyncio
import re
import wave
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soxr

from slate.board import ROOT
from slate.breadboard import breadboard, build_image
from slate.link import Link, open_board
from slate.voice.audio import MAX_AUDIO_SECONDS

RATE = 16_000
KEYS = {"left": "l", "right": "r", "mix": "m"}
SPEAKER_WAV = ROOT / ".local/reply.wav"
SPEAKER_FLOOR = 256
PLAYOUT_SECONDS = 1.5


@dataclass
class FirmwareTurn:
    transcript: str
    reply: str
    reply_audio_bytes: int
    speaker_seconds: float | None = None


def stereo_pcm(path: Path, slot: int = 0) -> bytes:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        rate = audio.getframerate()
        count = audio.getnframes()
        if (
            channels not in (1, 2)
            or audio.getsampwidth() != 2
            or rate not in (16000, 24000, 48000)
        ):
            raise ValueError("Use a mono/stereo 16-bit WAV at 16, 24, or 48 kHz")
        if not 0 < count <= rate * (MAX_AUDIO_SECONDS - 1):
            raise ValueError("Use a nonempty WAV of at most 119 seconds")
        data = audio.readframes(count)
        if len(data) != count * channels * 2:
            raise ValueError("WAV data is truncated")
    samples = np.frombuffer(data, dtype="<i2").reshape(-1, channels)
    if rate != RATE:
        samples = soxr.resample(samples, rate, RATE).astype("<i2")
    if channels == 1:
        stereo = np.zeros((len(samples), 2), dtype="<i2")
        stereo[:, slot] = samples[:, 0]
        samples = stereo
    padding = bytes(RATE * 4 // 5)
    return padding + samples.tobytes() + padding


def save_speaker(pcm: bytes) -> float:
    """Save what the firmware drove onto the I2S speaker pins, trimmed to the
    audible part, and return how many seconds of it were audible."""
    frames = np.frombuffer(pcm[: len(pcm) // 4 * 4], dtype="<i2").reshape(-1, 2)
    loud = np.flatnonzero(np.abs(frames).max(axis=1) > SPEAKER_FLOOR)
    if not loud.size:
        return 0.0
    audible = frames[loud[0] : loud[-1] + 1]
    SPEAKER_WAV.parent.mkdir(exist_ok=True)
    with wave.open(str(SPEAKER_WAV), "wb") as audio:
        audio.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        audio.writeframes(audible.tobytes())
    return len(audible) / RATE


async def speak_turn(link: Link, channel: str, sound: Awaitable[None]) -> FirmwareTurn:
    """Talk to the firmware the way a person would: hold Listen while speaking,
    then release. The firmware streams to the cloud on its own."""
    link.type(KEYS[channel])
    await link.wait_for("slate.mic.channel:")
    transcript = asyncio.ensure_future(link.wait_for("slate.transcript:", 180))
    reply = asyncio.ensure_future(link.wait_for("slate.reply:", 600))
    audio = asyncio.ensure_future(link.wait_for("slate.reply.audio:", 660))
    try:
        link.type("1")
        await link.wait_for("slate.state: 1")
        await sound
        link.type("3")
        heard = (await transcript).split("slate.transcript:", 1)[1].strip()
        said = (await reply).split("slate.reply:", 1)[1].strip()
        received = re.search(r"(\d+) bytes", await audio)
        if received is None:
            raise RuntimeError("slate.voice: firmware did not report reply audio")
        return FirmwareTurn(heard, said, int(received.group(1)))
    finally:
        for waiter in (transcript, reply, audio):
            waiter.cancel()
        await asyncio.gather(transcript, reply, audio, return_exceptions=True)


async def simulate_firmware(input_file: Path, channel: str = "left") -> FirmwareTurn:
    await asyncio.to_thread(build_image)
    played: asyncio.Queue[bytes] = asyncio.Queue()
    async with breadboard(realtime=True) as bench:
        bench.speaker_listeners.add(played)
        await bench.link.wait_for("slate.cloud: connected", 90)
        stereo = stereo_pcm(input_file, bench.slot)
        turn = await speak_turn(bench.link, channel, bench.play(stereo))
        await asyncio.sleep(PLAYOUT_SECONDS)
        bench.speaker_listeners.discard(played)
    blocks = []
    while not played.empty():
        blocks.append(played.get_nowait())
    turn.speaker_seconds = save_speaker(b"".join(blocks))
    return turn


async def speaker(path: Path) -> None:
    await asyncio.sleep(0.8)
    player = await asyncio.create_subprocess_exec("afplay", str(path))
    if await player.wait():
        raise RuntimeError(f"slate.voice: afplay could not play {path}")
    await asyncio.sleep(0.8)


async def board_firmware(input_file: Path, channel: str, port: str) -> FirmwareTurn:
    link, task = await open_board(port, ROOT / ".local/bench-serial.log")
    try:
        await link.wait_for("slate.cloud: connected", 60)
        return await speak_turn(link, channel, speaker(input_file))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
