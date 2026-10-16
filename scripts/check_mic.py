import asyncio
import struct
from ctypes import c_float

from hypothesis import example, given, settings
from hypothesis import strategies as st
from slate.board import load
from slate.breadboard import Breadboard, breadboard, build_image

KEYS = {"left": "l", "right": "r", "mix": "m"}


def reference(samples: list[tuple[int, int]], channel: str) -> list[int]:
    previous_input = previous_output = 0.0
    output = []
    for left, right in samples:
        if channel == "mix":
            value = (left + right) / 2
        elif channel == "left":
            value = left
        else:
            value = right
        filtered = c_float(
            c_float(value - previous_input).value
            + c_float(c_float(0.995).value * previous_output).value
        ).value
        previous_input, previous_output = value, filtered
        output.append(int(max(-32768, min(32767, filtered))))
    return output


def trimmed(samples: list[int]) -> list[int]:
    start = next((i for i, sample in enumerate(samples) if sample), len(samples))
    return samples[start:]


async def record(
    bench: Breadboard, samples: list[tuple[int, int]], channel: str, rate: int
) -> list[int]:
    link = bench.link
    link.type(KEYS[channel] + "a1")
    await link.wait_for("slate.state: 1")
    while not link.audio.empty():
        link.audio.get_nowait()
    lead = rate // 4
    stereo = [(0, 0)] * lead + samples + [(0, 0)] * 320
    await bench.play(b"".join(struct.pack("<hh", *pair) for pair in stereo))
    await bench.sleep(0.3)
    link.type("x0")
    await link.wait_for("slate.state: 0")
    chunks = []
    while not link.audio.empty():
        chunks.append(link.audio.get_nowait())
    audio = b"".join(chunks)
    return list(struct.unpack(f"<{len(audio) // 2}h", audio))


def main() -> None:
    build_image()
    board, _ = load()
    rate = board["device"]["mic"]["sample_hz"]
    with asyncio.Runner() as runner:
        context = breadboard()
        bench = runner.run(context.__aenter__())
        try:

            @settings(max_examples=20, deadline=None, print_blob=True)
            @example(samples=[(32767, -32768)] * 640, channel="mix")
            @given(
                samples=st.lists(
                    st.tuples(st.integers(-32768, 32767), st.integers(-32768, 32767)),
                    min_size=1,
                    max_size=640,
                ),
                channel=st.sampled_from(list(KEYS)),
            )
            def check(samples, channel):
                expected = trimmed(reference(samples, channel))
                actual = trimmed(runner.run(record(bench, samples, channel, rate)))
                assert len(actual) >= len(expected), "slate.mic: firmware lost samples"
                assert all(
                    abs(a - b) <= 1
                    for a, b in zip(actual[: len(expected)], expected, strict=True)
                ), "slate.mic: firmware output differs from the DC filter reference"
                replay = trimmed(runner.run(record(bench, samples, channel, rate)))
                span = len(expected) + 320
                assert replay[:span] == actual[:span], (
                    "slate.mic: the same input changed on replay"
                )

            check()

            async def cancelled_turn():
                link = bench.link
                link.type("l1")
                await link.wait_for("slate.state: 1")
                loud = asyncio.create_task(
                    bench.play(struct.pack("<hh", 30000, 0) * rate)
                )
                await bench.sleep(0.2)
                link.type("20")
                await link.wait_for("slate.state: 0")
                await loud
                await bench.sleep(0.3)
                samples = [(1000, -2000)] * 64
                actual = trimmed(await record(bench, samples, "left", rate))
                assert actual[:64] == reference(samples, "left"), (
                    "slate.mic: filter state leaked out of a muted turn"
                )

            runner.run(cancelled_turn())
            print(
                "slate.mic: flashed image in QEMU matches the DC filter reference for "
                "generated audio on all channels, replays identically, and resets "
                "after a muted turn"
            )
        finally:
            runner.run(context.__aexit__(None, None, None))


if __name__ == "__main__":
    main()
