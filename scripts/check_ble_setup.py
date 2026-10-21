import asyncio
import json

from check_cloud_link import line, receive
from slate.bluetooth import COMMAND, SERVICE, STATUS, AttError, PairingError
from slate.breadboard import breadboard, build_image
from websockets.asyncio.server import serve

TOKEN = "slate-ble-fixture"


def fields(data: bytes) -> dict[int, bytes]:
    result = {}
    while data:
        size = data[0]
        if size == 0 or size >= len(data):
            raise ValueError("slate.ble.check: malformed advertising data")
        result[data[1]] = data[2 : size + 1]
        data = data[size + 1 :]
    return result


async def passkey(bench, after: int) -> int:
    async with bench.timeout(10):
        while True:
            for entry in bench.link.lines[after:]:
                if entry.startswith("slate.ble: passkey "):
                    return int(entry.rsplit(" ", 1)[1])
            await bench.sleep(0.01)


async def check() -> None:
    accepted = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def handler(socket):
        assert socket.request.headers["Authorization"] == f"Bearer {TOKEN}"
        if not accepted.done():
            accepted.set_result(socket)
        await release.wait()

    async with serve(handler, "127.0.0.1", 0, compression=None) as server:
        port = server.sockets[0].getsockname()[1]
        try:
            async with breadboard(provision_cloud=False) as bench:
                phone = bench.bluetooth
                await line(bench.link, "slate.ble: advertising")
                adv = fields(phone.advertising_data)
                scan = fields(phone.scan_response)
                assert adv[1][0] & 3 == 2
                name = adv[9].decode()
                assert name == f"Slate-{phone.address[1]:02X}{phone.address[0]:02X}"
                assert scan[7] == SERVICE.bytes[::-1]
                phone.connect()
                await line(bench.link, "slate.ble: connected")
                assert await phone.exchange_mtu() == 256
                handles = await phone.discover()
                assert COMMAND in handles and STATUS in handles
                try:
                    await phone.write(handles[COMMAND], b"net eth")
                except AttError as error:
                    assert error.code in (5, 15), error
                else:
                    raise ValueError("slate.ble.check: unpaired write was accepted")

                async def wrong_passkey(after: int) -> int:
                    return await passkey(bench, after) ^ 1

                seen = len(bench.link.lines)
                try:
                    await phone.pair(lambda: wrong_passkey(seen))
                except PairingError:
                    await line(bench.link, "slate.ble: pairing failed")
                else:
                    raise ValueError("slate.ble.check: wrong passkey was accepted")
                if phone.connected:
                    phone.disconnect()
                async with bench.timeout(10):
                    while not phone.advertising:
                        await bench.sleep(0.01)
                seen = len(bench.link.lines)
                phone.connect()
                await phone.pair(lambda: passkey(bench, seen))
                assert await phone.exchange_mtu() == 256
                await line(bench.link, "slate.ble: paired")
                assert await phone.read(handles[STATUS]) == b"ready"
                await phone.subscribe(handles[STATUS])
                statuses = []

                async def status_until(predicate):
                    async with bench.timeout(30):
                        while not predicate():
                            handle, value = await phone.notifications.get()
                            assert handle == handles[STATUS]
                            statuses.append(value)

                await phone.write(handles[COMMAND], b"net eth")
                await status_until(
                    lambda: any(value.startswith(b"net up ") for value in statuses)
                )
                await phone.write(
                    handles[COMMAND], f"cloud ws://10.0.2.2:{port} {TOKEN}".encode()
                )
                await status_until(lambda: b"done" in statuses)
                assert b"saved net" in statuses, statuses
                assert b"saved cloud" in statuses, statuses
                assert any(value.startswith(b"net up ") for value in statuses), statuses
                await line(bench.link, "slate.ble: stopped")
                async with asyncio.timeout(30):
                    socket = await accepted
                    hello = await receive(socket, "hello")
                assert hello["mcu"] == "esp32-wroom-32", json.dumps(hello)
                commands = ",".join(
                    f"{opcode:04x}" for opcode in sorted(phone.commands)
                )
                print(
                    f"slate.ble.check: advertising, ATT security, wrong passkey, "
                    f"Secure Connections, provisioning, notifications, cloud hello; "
                    f"HCI={commands}"
                )
        finally:
            release.set()


def main() -> None:
    build_image()
    asyncio.run(check())


if __name__ == "__main__":
    main()
