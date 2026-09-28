import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path

from livekit import rtc

from slate.breadboard import Breadboard, breadboard, build_image
from slate.voice.audio import MAX_AUDIO_SECONDS
from slate.voice.device import transcribe_audio

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
    bench: Breadboard, stereo: bytes, channel: str
) -> AsyncIterator[bytes]:
    link = bench.link
    await link.type(KEYS[channel] + "a1")
    await link.wait_for("slate.state: 1")
    player = asyncio.create_task(bench.play(stereo))
    remaining = len(stereo) // 4
    try:
        while remaining > 0:
            chunk = await asyncio.wait_for(link.audio.get(), timeout=5)
            chunk = chunk[: remaining * 2]
            remaining -= len(chunk) // 2
            yield chunk
        await player
    finally:
        player.cancel()
        await link.type("x0")


async def simulate_firmware(input_file: Path, api_url: str, channel: str) -> str:
    await asyncio.to_thread(build_image)
    async with breadboard() as bench:
        stereo = stereo_pcm(input_file, bench.slot)
        return await transcribe_audio(capture(bench, stereo, channel), api_url, RATE)
