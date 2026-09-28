import asyncio
import struct
import zlib

from hypothesis import given, settings
from hypothesis import strategies as st
from slate.board import ROOT
from slate.breadboard import Breadboard, Frame, breadboard, build_image

COLORS = [0xFFFF, 0x07E0, 0, 0x001F, 0xFFE0, 0xF81F]
CENTER = 63 * 128 + 63


def check_orb(frame: Frame, state: int) -> None:
    colors = frame.pixels
    if state == 2:
        assert not any(colors), "slate.display: mute must blank the panel"
        return
    assert colors[CENTER] == COLORS[state], "slate.display: orb center has wrong color"
    assert all(color & ~COLORS[state] == 0 for color in colors)
    assert 2000 < sum(color != 0 for color in colors) < 7000
    for y in range(128):
        row = colors[y * 128 : (y + 1) * 128]
        assert row == row[::-1], "slate.display: orb must be horizontally centered"
        assert row == colors[(127 - y) * 128 : (128 - y) * 128]
        assert row[0] == row[-1] == 0, "slate.display: glow must fit within the panel"


async def show(bench: Breadboard, state: int) -> Frame:
    await bench.link.type(str(state))
    if bench.link.state() != state:
        await bench.link.wait_for(f"slate.state: {state}")
    count = bench.oled.count
    while bench.oled.count < count + 2:
        await asyncio.sleep(0.01)
    return bench.oled.frames[-1]


def save_png(frame: Frame) -> None:
    rows = bytearray()
    for y in range(128):
        rows.append(0)
        for color in frame.pixels[y * 128 : (y + 1) * 128]:
            r, g, b = color >> 11, (color >> 5) & 63, color & 31
            rows.extend(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))

    def chunk(kind, value):
        crc = struct.pack(">I", zlib.crc32(kind + value))
        return struct.pack(">I", len(value)) + kind + value + crc

    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(rows))
    image += chunk(b"IEND", b"")
    path = ROOT / ".local/display-listen.png"
    path.write_bytes(image)
    print(f"slate.display: captured {path}")


def main() -> None:
    build_image()
    with asyncio.Runner() as runner:
        context = breadboard()
        bench = runner.run(context.__aenter__())
        try:
            for state in range(6):
                check_orb(runner.run(show(bench, state)), state)

            first = runner.run(show(bench, 1))
            start = bench.oled.count
            runner.run(asyncio.sleep(4))
            frames = list(bench.oled.frames)[-(bench.oled.count - start) :]
            lit = [sum(1 for pixel in frame.pixels if pixel) for frame in frames]
            assert max(lit) > 1.3 * min(lit), "slate.display: orb is not breathing"
            for before, after in zip(frames, frames[1:], strict=False):
                step = max(
                    abs(((a >> 5) & 63) - ((b >> 5) & 63))
                    for a, b in zip(before.pixels, after.pixels, strict=True)
                )
                assert step <= 10, f"slate.display: orb jumped {step} green levels"
            save_png(first)

            @settings(max_examples=15, deadline=None, print_blob=True)
            @given(states=st.lists(st.integers(0, 5), min_size=1, max_size=5))
            def check(states):
                for state in states:
                    frame = runner.run(show(bench, state))
                check_orb(frame, states[-1])

            check()
            print(
                "slate.display: flashed image in QEMU draws all six states, blanks "
                "on mute, breathes smoothly, and follows 15 generated state sequences"
            )
        finally:
            runner.run(context.__aexit__(None, None, None))


if __name__ == "__main__":
    main()
