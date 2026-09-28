import asyncio

from slate.breadboard import breadboard, build_image

COLORS = [0xFFFF, 0x07E0, 0, 0x001F, 0xFFE0, 0xF81F]
CENTER = 63 * 128 + 63


async def run() -> None:
    async with breadboard() as bench:
        await asyncio.sleep(6)
    log = "\n".join(bench.serial)
    oled = bench.oled
    assert "slate.boot:" in log, f"slate.breadboard: firmware did not boot\n{log}"
    assert oled.on, "slate.breadboard: SSD1351 never received display on"
    assert len(oled.frames) > 20, f"slate.breadboard: only {len(oled.frames)} frames"
    state = bench.state()
    frame = oled.frames[-1].pixels
    assert frame[CENTER] == COLORS[state], (
        f"slate.breadboard: serial reports state {state} but the panel center is "
        f"{frame[CENTER]:#06x}"
    )
    assert frame[0] == 0, "slate.breadboard: orb glow reached the panel corner"
    recent = oled.frames[-20:]
    fps = (len(recent) - 1) / ((recent[-1].ns - recent[0].ns) / 1e9)
    print(log)
    print(
        f"slate.breadboard: {len(oled.frames)} SSD1351 frames decoded from SPI at "
        f"{fps:.1f} fps; panel color matches serial state {state}"
    )


def main() -> None:
    build_image()
    asyncio.run(run())


if __name__ == "__main__":
    main()
