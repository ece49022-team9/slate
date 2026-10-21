import argparse
import asyncio

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from slate.voice.device import cloud, socket_url

SERVICE = "5a7e0001-6a1b-4c3e-9f5d-8b2c1e0d4a10"
COMMAND = "5a7e0002-6a1b-4c3e-9f5d-8b2c1e0d4a10"
STATUS = "5a7e0003-6a1b-4c3e-9f5d-8b2c1e0d4a10"
SETUP_SECONDS = 90


def in_setup(device: BLEDevice, advertisement: AdvertisementData) -> bool:
    name = advertisement.local_name or device.name or ""
    return name.startswith("Slate-") or SERVICE in advertisement.service_uuids


async def set_up(ssid: str, password: str, scan_seconds: float) -> None:
    url, token = cloud()
    device = await BleakScanner.find_device_by_filter(in_setup, timeout=scan_seconds)
    if device is None:
        raise RuntimeError(
            "slate.pair: no Slate in setup mode nearby; send !setup over serial or "
            "erase its saved cloud settings"
        )
    statuses: asyncio.Queue[str] = asyncio.Queue()
    async with BleakClient(device) as client:
        print(
            f"slate.pair: connected to {device.name}; when macOS asks, enter the "
            "code shown on Slate's screen"
        )
        await client.start_notify(
            STATUS, lambda _, data: statuses.put_nowait(bytes(data).decode())
        )
        for line in (f"net wifi {ssid} {password}", f"cloud {socket_url(url)} {token}"):
            await client.write_gatt_char(COMMAND, line.encode(), response=True)
        async with asyncio.timeout(SETUP_SECONDS):
            while (status := await statuses.get()) != "done":
                print(f"slate.pair: {status}")
                if status.startswith("error"):
                    raise RuntimeError(f"slate.pair: Slate reported {status}")
    print(f"slate.pair: {device.name} saved Wi-Fi and the cloud address for {url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--scan-seconds", type=float, default=15)
    args = parser.parse_args()
    if " " in args.ssid or " " in args.password:
        parser.error("Wi-Fi names and passwords with spaces are not supported yet")
    asyncio.run(set_up(args.ssid, args.password, args.scan_seconds))


if __name__ == "__main__":
    main()
