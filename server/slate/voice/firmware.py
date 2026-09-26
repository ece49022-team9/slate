import asyncio
import os
import signal
import struct
import tempfile
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from livekit import rtc

from slate.voice.audio import MAX_AUDIO_SECONDS
from slate.voice.device import transcribe_audio

ROOT = Path(__file__).resolve().parents[3]
RATE = 16_000
FRAME_SAMPLES = 320
CHANNELS = {"left": 0, "right": 1, "mix": 2}


class Firmware:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer

    async def exchange(self, command: int, payload: bytes = b"") -> bytes:
        if len(payload) > FRAME_SAMPLES * 4:
            raise ValueError("slate.firmware: audio packet exceeds 20 ms")
        async with asyncio.timeout(10):
            self.writer.write(struct.pack("<BH", command, len(payload)) + payload)
            await self.writer.drain()
            status, size = struct.unpack("<BH", await self.reader.readexactly(3))
            if size > FRAME_SAMPLES * 2 or size % 2:
                raise RuntimeError("slate.firmware: invalid audio response length")
            audio = await self.reader.readexactly(size)
            if status:
                raise RuntimeError(
                    f"slate.firmware: command {command} failed: {status}"
                )
            return audio


@asynccontextmanager
async def qemu() -> AsyncIterator[Firmware]:
    with tempfile.TemporaryDirectory(prefix="slate-qemu-", dir="/tmp") as directory:
        socket = Path(directory) / "audio.sock"
        log_path = Path(directory) / "qemu.log"
        with log_path.open("w") as log:
            process = await asyncio.create_subprocess_exec(
                "bash",
                "scripts/esp-idf.sh",
                "qemu",
                "--qemu-extra-args",
                f"-serial unix:{socket},server=on,wait=off",
                cwd=ROOT,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            writer = None
            try:
                async with asyncio.timeout(60):
                    while "Audio bridge ready on UART1" not in log_path.read_text():
                        if any(
                            failure in log_path.read_text()
                            for failure in (
                                "assert failed:",
                                "stack overflow",
                                "Guru Meditation",
                            )
                        ):
                            raise RuntimeError("slate.firmware: firmware crashed")
                        if process.returncode is not None:
                            raise RuntimeError("slate.firmware: QEMU exited at startup")
                        await asyncio.sleep(0.1)
                reader, writer = await asyncio.open_unix_connection(socket)
                yield Firmware(reader, writer)
            except (TimeoutError, RuntimeError, asyncio.IncompleteReadError) as error:
                raise RuntimeError(
                    f"slate.firmware: simulation failed: {error}\n"
                    f"{log_path.read_text()}"
                ) from error
            finally:
                if writer is not None:
                    writer.close()
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()


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
