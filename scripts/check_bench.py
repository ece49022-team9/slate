import asyncio
import math
import struct
import sys
import wave

from slate.board import ROOT, load
from slate.link import Link, open_board

PORT_LOG = ROOT / ".local/bench-serial.log"
TONE = ROOT / ".local/bench-tone.wav"


def power(samples: list[int], hz: float, rate: int) -> float:
    coefficient = 2 * math.cos(2 * math.pi * hz / rate)
    previous = before = 0.0
    for sample in samples:
        previous, before = sample + coefficient * previous - before, previous
    return previous * previous + before * before - coefficient * previous * before


def rms(samples: list[int]) -> float:
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def write_tone(rate: int) -> None:
    samples = [
        round(12000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate * 3)
    ]
    TONE.parent.mkdir(exist_ok=True)
    with wave.open(str(TONE), "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(struct.pack(f"<{len(samples)}h", *samples))


async def listen(link: Link, key: str, seconds: float, sound: bool) -> list[int]:
    link.type(key + "a1")
    await link.wait_for("slate.state: 1")
    while not link.audio.empty():
        link.audio.get_nowait()
    player = None
    if sound:
        player = await asyncio.create_subprocess_exec("afplay", str(TONE))
    await asyncio.sleep(seconds)
    link.type("x0")
    await link.wait_for("slate.state: 0")
    if player:
        await player.wait()
    audio = b""
    while not link.audio.empty():
        audio += link.audio.get_nowait()
    samples = list(struct.unpack(f"<{len(audio) // 2}h", audio))
    return samples[len(samples) // 10 :]


async def run(port: str) -> None:
    board, _ = load()
    mic = board["device"]["mic"]
    rate = mic["sample_hz"]
    key = "lr"[["left", "right"].index(mic["slot"])]
    write_tone(rate)
    link, task = await open_board(port, PORT_LOG)
    try:
        for state in [1, 2, 3, 4, 5, 0]:
            link.type(str(state))
            await link.wait_for(f"slate.state: {state}", seconds=3)
        line = await link.wait_for("slate.oled:", seconds=5)
        fps = float(line.split("(")[1].split()[0])
        assert fps >= 30, f"slate.bench: OLED runs at {fps} fps"
        quiet = await listen(link, key, 1.5, sound=False)
        loud = await listen(link, key, 2.5, sound=True)
        assert len(loud) > rate, f"slate.bench: only {len(loud)} samples streamed"
        others = [power(loud, hz, rate) for hz in (300, 600, 1000, 2000)]
        ratio = power(loud, 440, rate) / (sum(others) / len(others))
        assert rms(loud) > 3 * rms(quiet), (
            f"slate.bench: tone rms {rms(loud):.0f} is not above "
            f"background {rms(quiet):.0f}"
        )
        assert ratio > 10, f"slate.bench: 440 Hz is only {ratio:.1f}x the other bands"
    finally:
        task.cancel()
    print(
        f"slate.bench: {board['mcu']} went through all six states; OLED at {fps} fps; "
        f"the {mic['slot']} mic heard the speaker's 440 Hz tone at rms "
        f"{rms(loud):.0f} (background {rms(quiet):.0f}), {ratio:.0f}x the other "
        "bands. Check the panel by eye: the orb should change color with each state."
    )


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
