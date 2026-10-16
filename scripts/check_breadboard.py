import asyncio
import hashlib
import math

from slate.board import load
from slate.breadboard import breadboard, build_image, tone

COLORS = [0xFFFF, 0x07E0, 0, 0x001F, 0xFFE0, 0xF81F]
CENTER = 63 * 128 + 63


def lit(frame) -> int:
    return sum(1 for pixel in frame.pixels if pixel)


def rms(line: str) -> float:
    return float(line.split("rms=")[1].split(",")[0])


async def run(rate: int) -> tuple[str, str]:
    async with breadboard(connect_cloud=False) as bench:
        link = bench.link
        await bench.sleep(1)
        idle = bench.oled.frames[-1]
        assert idle.pixels[CENTER] == COLORS[0], "slate.breadboard: idle orb is wrong"
        assert idle.data[:2] == b"\0\0", "slate.breadboard: glow reached the corner"

        link.type("l1")
        await link.wait_for("slate.state: 1")
        await bench.sleep(1)
        quiet = bench.oled.frames[-1]
        amplitude = 8000
        speech = asyncio.create_task(bench.speak(tone(440, amplitude, 3, rate)))
        await link.wait_for("slate.mic:")
        heard = rms(await link.wait_for("slate.mic:"))
        loud = bench.oled.frames[-1]
        await speech
        expected = amplitude / math.sqrt(2)
        assert abs(heard - expected) < 0.05 * expected, (
            f"slate.breadboard: left mic heard rms {heard:.0f}, expected {expected:.0f}"
        )
        assert lit(loud) > lit(quiet), "slate.breadboard: orb did not grow with sound"
        assert loud.pixels[CENTER] != COLORS[1], (
            "slate.breadboard: orb did not brighten"
        )

        link.type("0")
        await link.wait_for("slate.state: 0")
        link.type("r1")
        await link.wait_for("slate.state: 1")
        speech = asyncio.create_task(bench.speak(tone(440, amplitude, 2, rate)))
        await link.wait_for("slate.mic:")
        other = rms(await link.wait_for("slate.mic:"))
        await speech
        assert other < 50, f"slate.breadboard: right slot heard the left mic ({other})"
        link.type("0")
        await link.wait_for("slate.state: 0")

        frames = list(bench.oled.frames)
        fps = (len(frames[-20:]) - 1) / ((frames[-1].ns - frames[-20].ns) / 1e9)
        trace = hashlib.sha256("\n".join(link.lines).encode())
        for frame in frames:
            trace.update(frame.ns.to_bytes(8, "little") + frame.data)
        summary = (
            f"SSD1351 decoded from SPI at {fps:.1f} fps; I2S left slot heard rms "
            f"{heard:.0f} of {expected:.0f}; right slot {other:.0f}; orb grew from "
            f"{lit(quiet)} to {lit(loud)} pixels; {bench.oled.count} frames in "
            f"{bench.now / 1e9:.1f} emulated seconds"
        )
        return summary, trace.hexdigest()


def main() -> None:
    build_image()
    board, _ = load()
    rate = board["device"]["mic"]["sample_hz"]
    summary, first = asyncio.run(run(rate))
    build_image()
    _, second = asyncio.run(run(rate))
    assert first == second, (
        f"slate.breadboard: two runs with the same inputs differ: {first} {second}"
    )
    print(f"slate.breadboard: flashed image in QEMU; {summary}")
    print(
        f"slate.breadboard: a second run matched serial and every frame ({first[:16]})"
    )


if __name__ == "__main__":
    main()
