"""Validate the dedicated speaker-test firmware through QEMU I2S1 DMA."""

import asyncio
import math
import struct
import wave

RATE = 16_000
FREQUENCY = 440
AMPLITUDE = 20_000


def check_tone(pcm: bytes) -> tuple[int, float]:
    if len(pcm) % 4:
        raise ValueError("Speaker DMA output contains an incomplete stereo frame")
    samples = list(struct.iter_unpack("<hh", pcm))
    if not samples:
        raise ValueError("No speaker PCM received; rebuild QEMU with make sim-setup")
    start = next((i for i, pair in enumerate(samples) if pair != (0, 0)), None)
    if start is None:
        raise ValueError("Speaker output is entirely silent")
    # First sine sample is zero. Start at sample 1 and check the full tone,
    # spanning many DMA descriptors, against an independent waveform reference.
    frames = samples[start : start + RATE - 1]
    if len(frames) < RATE - 1:
        raise ValueError("Speaker output stopped before one second of tone")
    for i, (left, right) in enumerate(frames, 1):
        expected = int(AMPLITUDE * math.sin(2 * math.pi * FREQUENCY * i / RATE))
        if left != right:
            raise ValueError(f"Speaker channels differ at tone sample {i}")
        if abs(left - expected) > 32:
            raise ValueError(
                f"Speaker tone differs at sample {i}: {left}, expected {expected}"
            )
    rms = math.sqrt(sum(left * left for left, _ in frames) / len(frames))
    return len(frames), rms


def check_silence(pcm: bytes) -> None:
    check_tone(pcm)
    frames = list(struct.iter_unpack("<hh", pcm))
    start = next(i for i, pair in enumerate(frames) if pair != (0, 0))
    tail = frames[start + RATE - 1 :]
    if len(tail) < RATE // 4:
        raise ValueError("Need at least 250 ms of silence after speaker playback")
    if any(pair != (0, 0) for pair in tail):
        raise ValueError("Speaker output is not silent after playback")


async def check(combined: bool = False) -> None:
    from slate.board import ROOT
    from slate.breadboard import breadboard

    boot = "slate.state: 0" if combined else "slate.speaker: ready"
    async with breadboard(boot_message=boot, connect_cloud=False) as bench:
        if combined:
            bench.link.type("t")
        # Bound capture in emulated time; no firmware debug prints are needed.
        async with bench.timeout(4):
            await bench.sleep(2)
        check_silence(bytes(bench.speaker_pcm))
        pcm = bytes(bench.speaker_pcm)
        path = ROOT / ".local/speaker.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
            audio.writeframes(pcm)
        count, rms = check_tone(pcm)
        print(
            f"slate.speaker: checked {count} frames of 440 Hz stereo PCM, "
            f"rms={rms:.0f}, {len(bench.speaker_blocks)} DMA blocks; saved {path}"
        )


def main() -> None:
    from slate.breadboard import build_image

    for environment in ("speaker-test", "esp32dev"):
        build_image(environment)
        asyncio.run(check(combined=environment == "esp32dev"))


if __name__ == "__main__":
    main()
