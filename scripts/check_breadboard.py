import asyncio
import math

from slate.board import load
from slate.breadboard import breadboard, build_image, tone

COLORS = [0xFFFF, 0x07E0, 0, 0x001F, 0xFFE0, 0xF81F]
CENTER = 63 * 128 + 63


def lit(frame) -> int:
    return sum(1 for pixel in frame.pixels if pixel)


def rms(line: str) -> float:
    return float(line.split("rms=")[1].split(",")[0])


async def run(rate: int) -> None:
    async with breadboard() as bench:
        await bench.wait_for("slate.state: 0")
        await asyncio.sleep(1)
        idle = bench.oled.frames[-1]
        assert idle.pixels[CENTER] == COLORS[0], "slate.breadboard: idle orb is wrong"
        assert idle.data[:2] == b"\0\0", "slate.breadboard: glow reached the corner"

        await bench.type("l1")
        await bench.wait_for("slate.state: 1")
        await asyncio.sleep(1)
        quiet = bench.oled.frames[-1]
        amplitude = 8000
        speech = asyncio.create_task(bench.speak(tone(440, amplitude, 3, rate)))
        await bench.wait_for("slate.mic:")
        heard = rms(await bench.wait_for("slate.mic:"))
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

        await bench.type("0")
        await bench.wait_for("slate.state: 0")
        await bench.type("r1")
        await bench.wait_for("slate.state: 1")
        speech = asyncio.create_task(bench.speak(tone(440, amplitude, 2, rate)))
        await bench.wait_for("slate.mic:")
        other = rms(await bench.wait_for("slate.mic:"))
        await speech
        assert other < 50, f"slate.breadboard: right slot heard the left mic ({other})"
        await bench.type("0")

        recent = list(bench.oled.frames)[-20:]
        fps = (len(recent) - 1) / ((recent[-1].ns - recent[0].ns) / 1e9)
    print(
        f"slate.breadboard: flashed image in QEMU; SSD1351 decoded from SPI at "
        f"{fps:.1f} fps; I2S left slot heard rms {heard:.0f} of {expected:.0f}; "
        f"right slot {other:.0f}; orb grew from {lit(quiet)} to {lit(loud)} pixels"
    )


def main() -> None:
    build_image()
    board, _ = load()
    asyncio.run(run(board["device"]["mic"]["sample_hz"]))


if __name__ == "__main__":
    main()
