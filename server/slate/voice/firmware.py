import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path

from livekit import rtc

from slate.simulator import FRAME_SAMPLES, ROOT, Firmware, qemu
from slate.voice.audio import MAX_AUDIO_SECONDS
from slate.voice.device import transcribe_audio

RATE = 16_000
CHANNELS = {"left": 0, "right": 1, "mix": 2}


def stereo_pcm(path: Path) -> bytes:
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
            stereo[i * 4 : i * 4 + 2] = pcm[i * 2 : i * 2 + 2]
        pcm = bytes(stereo)
    padding = bytes(RATE * 4 // 5)
    return padding + pcm + padding


async def capture(
    firmware: Firmware, stereo: bytes, channel: str
) -> AsyncIterator[bytes]:
    await firmware.exchange(1, bytes([CHANNELS[channel]]))
    try:
        for offset in range(0, len(stereo), FRAME_SAMPLES * 4):
            chunk = stereo[offset : offset + FRAME_SAMPLES * 4]
            audio = await firmware.exchange(2, chunk)
            if len(audio) != len(chunk) // 2:
                raise RuntimeError("slate.firmware: capture lost samples")
            yield audio
        if tail := await firmware.exchange(3):
            yield tail
    finally:
        await firmware.exchange(4)


async def simulate_firmware(input_file: Path, api_url: str, channel: str) -> str:
    stereo = stereo_pcm(input_file)
    build = await asyncio.create_subprocess_exec(
        "bash", "scripts/esp-idf.sh", "build", cwd=ROOT
    )
    if await build.wait():
        raise RuntimeError("slate.firmware: build failed")
    async with qemu() as firmware:
        return await transcribe_audio(capture(firmware, stereo, channel), api_url, RATE)
