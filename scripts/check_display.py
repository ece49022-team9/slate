import asyncio
import math
import struct
import subprocess
import zlib

from hypothesis import given, settings
from hypothesis import strategies as st
from slate.simulator import ROOT, Firmware, qemu

COLORS = [0xFFFF, 0x07E0, 0, 0x001F, 0xFFE0, 0xF81F]


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
                colors = struct.unpack("<16384H", data)
                assert set(colors) <= {0, COLORS[state]}
                assert sum(color != 0 for color in colors) == (0 if state == 2 else 128)
                if state != 2:
                    assert colors[96 * 128 + 16] == COLORS[state]
                    assert colors[32 * 128 + 48] == COLORS[state]
            first = runner.run(snapshot(firmware, 1, 0))
            assert first != runner.run(snapshot(firmware, 1, 10))
            save_png(first)

            @settings(max_examples=30, deadline=None, print_blob=True)
            @given(state=st.integers(0, 5), number=st.integers(0, 100000))
            def check(state, number):
                data = runner.run(snapshot(firmware, state, number))
                assert data == runner.run(snapshot(firmware, state, number))
                colors = struct.unpack("<16384H", data)
                assert set(colors) <= {0, COLORS[state]}
                if state == 2:
                    assert not any(colors)
                    return
                for x in range(128):
                    ys = [y for y in range(128) if colors[y * 128 + x]]
                    assert len(ys) == 1
                    expected = 63.5 + 32 * math.sin(math.tau * x / 64 - number * 0.1)
                    assert abs(ys[0] - expected) <= 0.501

            check()
            print(
                "slate.display: six states, animation, "
                "and 30 generated pixel cases passed"
            )
        finally:
            runner.run(context.__aexit__(None, None, None))


if __name__ == "__main__":
    main()
