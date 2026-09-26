import asyncio
import struct
import subprocess
from ctypes import c_float

from hypothesis import example, given, settings
from hypothesis import strategies as st
from slate.voice.firmware import CHANNELS, ROOT, Firmware, capture, qemu


def reference(samples: list[tuple[int, int]], channel: str) -> list[int]:
    previous_input = previous_output = 0.0
    output = []
    for left, right in samples:
        value = (
            (left + right) / 2
            if channel == "mix"
            else (left if channel == "left" else right)
        )
        filtered = c_float(
            c_float(value - previous_input).value
            + c_float(c_float(0.995).value * previous_output).value
        ).value
        previous_input, previous_output = value, filtered
        output.append(int(max(-32768, min(32767, filtered))))
    return output


async def record(
    firmware: Firmware, samples: list[tuple[int, int]], channel: str, chunk_size: int
) -> bytes:
    await firmware.exchange(1, bytes([CHANNELS[channel]]))
    audio = bytearray()
    try:
        for offset in range(0, len(samples), chunk_size):
            chunk = samples[offset : offset + chunk_size]
            packed = b"".join(struct.pack("<hh", *pair) for pair in chunk)
            audio.extend(await firmware.exchange(2, packed))
        audio.extend(await firmware.exchange(3))
        return bytes(audio)
    finally:
        await firmware.exchange(4)


def main() -> None:
    subprocess.run(["bash", "scripts/esp-idf.sh", "build"], cwd=ROOT, check=True)
    with asyncio.Runner() as runner:
        context = qemu()
        firmware = runner.run(context.__aenter__())
        try:

            @settings(max_examples=60, deadline=None, print_blob=True)
            @example(samples=[(32767, -32768)] * 640, channel="mix", chunk_size=17)
            @given(
                samples=st.lists(
                    st.tuples(st.integers(-32768, 32767), st.integers(-32768, 32767)),
                    min_size=1,
                    max_size=640,
                ),
                channel=st.sampled_from(list(CHANNELS)),
                chunk_size=st.integers(1, 320),
            )
            def check(samples, channel, chunk_size):
                audio = runner.run(record(firmware, samples, channel, chunk_size))
                replay = runner.run(record(firmware, samples, channel, 320))
                assert audio == replay, "Audio depends on chunk boundaries or old turns"
                actual = struct.unpack(f"<{len(samples)}h", audio)
                expected = reference(samples, channel)
                assert all(
                    abs(a - b) <= 1 for a, b in zip(actual, expected, strict=True)
                )

            check()

            async def cancel_capture():
                stream = capture(
                    firmware, struct.pack("<hh", 1000, -2000) * 640, "left"
                )
                await anext(stream)
                await stream.aclose()

            runner.run(cancel_capture())
            try:
                runner.run(firmware.exchange(2, struct.pack("<hh", 1000, -1000)))
            except RuntimeError as error:
                assert "command 2 failed: 1" in str(error)
            else:
                raise AssertionError("Audio accepted after turn reset")
            print(
                "slate.mic: QEMU property checks passed: "
                "PCM oracle, chunk replay, cancellation"
            )
        finally:
            runner.run(context.__aexit__(None, None, None))


if __name__ == "__main__":
    main()
