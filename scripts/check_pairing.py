import asyncio
import os
import tempfile
from unittest.mock import patch

import httpx
import uvicorn
from check_ble_setup import passkey
from check_cloud_link import line
from slate.bluetooth import COMMAND, STATUS
from slate.breadboard import breadboard, build_image

OWNER = "slate-pairing-owner"


class IdleAgent:
    pending = set()
    timings = {}
    last_run = {}

    async def run(self, message, progress, *, device_context):
        return "Ready."

    async def close(self):
        pass


async def provision(bench, url: str, token: str) -> None:
    phone = bench.bluetooth
    await line(bench.link, "slate.ble: advertising")
    phone.connect()
    await line(bench.link, "slate.ble: connected")
    handles = await phone.discover()
    seen = len(bench.link.lines)
    await phone.pair(lambda: passkey(bench, seen))
    await line(bench.link, "slate.ble: paired")
    await phone.subscribe(handles[STATUS])
    statuses = []

    async def status_until(predicate):
        async with bench.timeout(30):
            while not predicate():
                handle, value = await phone.notifications.get()
                assert handle == handles[STATUS]
                statuses.append(value)

    await phone.write(handles[COMMAND], b"net eth")
    await status_until(lambda: any(v.startswith(b"net up ") for v in statuses))
    await phone.write(handles[COMMAND], f"cloud {url} {token}".encode())
    await status_until(lambda: b"done" in statuses)
    assert b"saved cloud" in statuses, statuses


async def wait_until(predicate, seconds: float, what: str) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.05)
    print(f"slate.pair.check: {what}")


async def check() -> None:
    from slate.app import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(20):
        while not server.started:
            await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    voice = app.state.voice
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {OWNER}"},
        ) as api:
            async with breadboard(provision_cloud=False) as bench:
                name = f"Slate-{bench.bluetooth.address[1]:02X}"
                reply = await api.post("/api/devices", json={"name": name})
                reply.raise_for_status()
                paired = reply.json()
                device_id = paired["device"]["id"]
                assert paired["token"].startswith("sdt_")
                url = f"ws://10.0.2.2:{port}/api/device/socket"
                await provision(bench, url, paired["token"])
                await line(bench.link, "slate.cloud: connected", 30)
                await wait_until(
                    lambda: (
                        voice.current is not None
                        and voice.current.device_id == device_id
                    ),
                    20,
                    f"firmware connected as {device_id} with its own token",
                )
                listed = (await api.get("/api/devices")).json()
                assert listed[0]["last_seen_at"] is not None, listed
                session = voice.current
                reply = await api.delete(f"/api/devices/{device_id}")
                reply.raise_for_status()
                await line(bench.link, "slate.cloud: disconnected", 20)
                assert session.closed
                print("slate.pair.check: revoking closed the firmware socket")
                await bench.sleep(10)
                assert voice.current is session, "a revoked device reconnected"
                print("slate.pair.check: firmware retries were refused for 10 s")
    finally:
        server.should_exit = True
        await serving


def main() -> None:
    build_image()
    with (
        tempfile.TemporaryDirectory() as home,
        patch.dict(os.environ, {"SLATE_DEVICE_TOKEN": OWNER, "SLATE_HOME": home}),
        patch("slate.voice.session.create_agent", return_value=IdleAgent()),
    ):
        asyncio.run(check())
    print(
        "slate.pair.check: per-device token over Bluetooth, firmware socket, "
        "revoke and lockout"
    )


if __name__ == "__main__":
    main()
