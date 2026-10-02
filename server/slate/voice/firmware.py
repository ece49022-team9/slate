import asyncio
import wave
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path

from livekit import rtc

from slate.board import ROOT
from slate.breadboard import breadboard, build_image
from slate.link import Link, open_board
from slate.voice.audio import MAX_AUDIO_SECONDS
from slate.voice.device import VoiceResult, transcribe_audio
from slate.voice.timing import Timeline

RATE = 16_000
KEYS = {"left": "l", "right": "r", "mix": "m"}


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
    pcm = data
    if rate != RATE:
        resampler = rtc.AudioResampler(rate, RATE, num_channels=channels)
        converted = resampler.push(rtc.AudioFrame(data, rate, channels, count))
        converted += resampler.flush()
        pcm = b"".join(frame.data.tobytes() for frame in converted)
    if channels == 1:
        count = len(pcm) // 2
        stereo = bytearray(count * 4)
        for i in range(count):
            start = i * 4 + slot * 2
            stereo[start : start + 2] = pcm[i * 2 : i * 2 + 2]
        pcm = bytes(stereo)
    padding = bytes(RATE * 4 // 5)
    return padding + pcm + padding


async def capture(
    link: Link, channel: str, samples: int, sound: Awaitable[None]
) -> AsyncIterator[bytes]:
    link.type(KEYS[channel] + "a1")
    await link.wait_for("slate.state: 1")
    while not link.audio.empty():
        link.audio.get_nowait()
    player = asyncio.ensure_future(sound)
    try:
        while samples > 0:
            chunk = await asyncio.wait_for(link.audio.get(), timeout=5)
            chunk = chunk[: samples * 2]
            samples -= len(chunk) // 2
            yield chunk
        await player
    finally:
        player.cancel()
        link.type("x0")


async def simulate_firmware(
    input_file: Path, api_url: str, channel: str, *, profile: bool = False
) -> VoiceResult:
    timing = Timeline()
    timing.mark("build_requested")
    await asyncio.to_thread(build_image)
    timing.mark("build_completed")
    async with breadboard(realtime=True) as bench:
        timing.mark("board_ready")
        stereo = stereo_pcm(input_file, bench.slot)
        audio = capture(bench.link, channel, len(stereo) // 4, bench.play(stereo))
        result = await transcribe_audio(audio, api_url, RATE, profile=profile)
        result.timings["simulator"] = timing.snapshot()
        return result


async def speaker(path: Path) -> None:
    await asyncio.sleep(0.8)
    player = await asyncio.create_subprocess_exec("afplay", str(path))
    if await player.wait():
        raise RuntimeError(f"slate.voice: afplay could not play {path}")
    await asyncio.sleep(0.8)


async def board_firmware(
    input_file: Path, api_url: str, channel: str, port: str
) -> VoiceResult:
    with wave.open(str(input_file), "rb") as audio:
        seconds = audio.getnframes() / audio.getframerate()
    link, task = await open_board(port, ROOT / ".local/bench-serial.log")
    try:
        samples = int((seconds + 1.6) * RATE)
        audio = capture(link, channel, samples, speaker(input_file))
        return await transcribe_audio(audio, api_url, RATE)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
