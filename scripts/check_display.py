import asyncio
import struct
import subprocess
import zlib

from hypothesis import given, settings
from hypothesis import strategies as st
from slate.simulator import ROOT, Firmware, qemu

COLORS = [0xFFFF, 0x07E0, 0, 0x001F, 0xFFE0, 0xF81F]


def check_orb(data: bytes, state: int):
    colors = struct.unpack("<16384H", data)
    if state == 2:
        assert not any(colors)
        return
    assert colors[63 * 128 + 63] == COLORS[state], "Orb center must be lit"
    assert all(color & ~COLORS[state] == 0 for color in colors)
    assert 2000 < sum(color != 0 for color in colors) < 7000
    for y in range(128):
        row = colors[y * 128 : (y + 1) * 128]
        assert row == row[::-1], "Orb must be horizontally centered"
        assert row == colors[(127 - y) * 128 : (128 - y) * 128]
        assert row[0] == row[-1] == 0, "Glow must fit within the panel"
    assert not any(colors[:128])
    middle = colors[63 * 128 : 64 * 128]
    assert all(a <= b for a, b in zip(middle[:63], middle[1:64], strict=True))


async def snapshot(firmware: Firmware, state: int, number: int) -> bytes:
    await firmware.exchange(6, bytes([state]))
    data = await firmware.exchange(5, struct.pack("<I", number))
    assert struct.unpack("<HHII", data[:12]) == (128, 128, state, number)
    return data[12:]


def save_png(data: bytes):
    rows = bytearray()
    colors = struct.unpack("<16384H", data)
    for y in range(128):
        rows.append(0)
        for color in colors[y * 128 : (y + 1) * 128]:
            r, g, b = color >> 11, (color >> 5) & 63, color & 31
            rows.extend(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))

    def chunk(kind, value):
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 0))
    image += chunk(b"IDAT", zlib.compress(rows))
    image += chunk(b"IEND", b"")
    path = ROOT / ".local/display-listen.png"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(image)
    print(f"slate.display: captured {path}")


def main():
    subprocess.run(["bash", "scripts/esp-idf.sh", "build"], cwd=ROOT, check=True)
    with asyncio.Runner() as runner:
        context = qemu()
        firmware = runner.run(context.__aenter__())
        try:
            for state in range(6):
                data = runner.run(snapshot(firmware, state, 0))
                check_orb(data, state)
            first = runner.run(snapshot(firmware, 1, 0))
            assert first != runner.run(snapshot(firmware, 1, 10))
            expanded = struct.unpack("<16384H", runner.run(snapshot(firmware, 1, 30)))
            contracted = struct.unpack("<16384H", runner.run(snapshot(firmware, 1, 90)))
            assert sum(bool(c) for c in expanded) > 1.3 * sum(
                bool(c) for c in contracted
            )
            assert all(a >= b for a, b in zip(expanded, contracted, strict=True))
            previous = None
            for number in range(121):
                data = runner.run(snapshot(firmware, 1, number))
                levels = [(c >> 5) & 63 for c in struct.unpack("<16384H", data)]
                if previous is not None:
                    assert (
                        max(abs(a - b) for a, b in zip(levels, previous, strict=True))
                        <= 5
                    )
                previous = levels
            assert data == first, "Animation must loop without a jump"
            save_png(first)

            @settings(max_examples=30, deadline=None, print_blob=True)
            @given(state=st.integers(0, 5), number=st.integers(0, 2**32 - 121))
            def check(state, number):
                data = runner.run(snapshot(firmware, state, number))
                assert data == runner.run(snapshot(firmware, state, number))
                assert data == runner.run(snapshot(firmware, state, number + 120))
                check_orb(data, state)

            check()
            runner.run(firmware.exchange(6, bytes([0])))
            runner.run(firmware.exchange(1, bytes([0])))
            speech = struct.pack("<hh", 3000, 3000) * 320
            for _ in range(5):
                runner.run(firmware.exchange(2, speech))
            reactive = runner.run(snapshot(firmware, 1, 0))
            quiet_pixels = struct.unpack("<16384H", first)
            reactive_pixels = struct.unpack("<16384H", reactive)
            assert reactive_pixels[63 * 128 + 63] != COLORS[1]
            lit = sum(bool(c) for c in reactive_pixels)
            assert lit > sum(bool(c) for c in quiet_pixels)
            muted = runner.run(snapshot(firmware, 2, 0))
            assert not any(muted)
            assert runner.run(snapshot(firmware, 1, 0)) == first
            print(
                "slate.display: six states, glowing orb, audio response, "
                "smooth breathing cycle, and 30 generated pixel cases passed"
            )
        finally:
            runner.run(context.__aexit__(None, None, None))


if __name__ == "__main__":
    main()
