import asyncio

from slate.breadboard import breadboard, build_image
from slate.device import DeviceSDK, FirmwareDevice, OrbRequest, TextRequest


async def run() -> None:
    async with breadboard(connect_cloud=False) as bench:
        active = True
        peer = FirmwareDevice(bench.link, lambda: "fixture")

        async def execute(command):
            return (await peer.execute(command)).model_dump()

        sdk = DeviceSDK("fixture", "fixture", execute, lambda: active)
        initial = await sdk.get_status()
        small = await sdk.set_orb(OrbRequest(color="#0000ff", radius=12))
        await bench.sleep(0.1)
        small_pixels = bench.oled.frames[-1].pixels
        assert small_pixels[63 * 128 + 63] == 0x001F
        large = await sdk.set_orb(OrbRequest(color="#0000ff", radius=40))
        await bench.sleep(0.1)
        large_pixels = bench.oled.frames[-1].pixels
        assert sum(bool(pixel) for pixel in large_pixels) > 2 * sum(
            bool(pixel) for pixel in small_pixels
        ), "Firmware did not resize the orb"
        text = await sdk.show_text(TextRequest(text="A"))
        await bench.sleep(0.1)
        pixels = bench.oled.frames[-1].pixels
        for column, bits in enumerate((0x7C, 0x12, 0x11, 0x12, 0x7C)):
            for row in range(8):
                expected = 0xFFFF if bits & (1 << row) else 0
                assert pixels[(96 + row) * 128 + 1 + column] == expected, (
                    "OLED SPI pixels do not match the requested A glyph"
                )
        status = await sdk.get_status()
        assert status.text == "A" and status.color == "#0000ff"
        assert initial.revision < small.revision < large.revision < text.revision
        request = "a" * 32
        waiting = asyncio.create_task(bench.link.wait_for(f"slate.device:{request} "))
        await asyncio.sleep(0)
        bench.link.type(f"@{request} orb ff0000 99\n")
        rejected = await waiting
        assert '"error"' in rejected, "Firmware accepted an invalid radius"
        waiting = asyncio.create_task(bench.link.wait_for(f"slate.device:{request} "))
        await asyncio.sleep(0)
        bench.link.type(f"@{request} text " + "41" * 65 + "\n")
        rejected = await waiting
        assert '"error"' in rejected, "Firmware silently truncated oversized text"
        oversized = asyncio.create_task(
            bench.link.wait_for("slate.device.error: command too long")
        )
        await asyncio.sleep(0)
        bench.link.type("@" + "0" * 250 + "\n")
        await oversized
        after = await sdk.get_status()
        assert after.color == status.color and after.revision == status.revision
        assert after.state == status.state, "Malformed command changed device state"
        active = False
        try:
            await sdk.show_text(TextRequest(text="Late"))
        except ValueError:
            pass
        else:
            raise AssertionError("Cancelled scope still controls the device")
        print(
            "slate.device: QEMU firmware acknowledged color, size and text; "
            "SPI pixels matched the glyph; malformed commands and stale scope rejected"
        )


def main() -> None:
    build_image()
    asyncio.run(run())


if __name__ == "__main__":
    main()
