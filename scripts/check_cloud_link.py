import argparse
import asyncio
import json
import os
import struct
from contextlib import contextmanager

from check_resources import check, factors, parse
from slate.breadboard import breadboard, build_image, tone
from slate.display import mic_slot_pcm
from slate.link import Link
from websockets.asyncio.server import serve

TOKEN = "slate-offline-fixture"


@contextmanager
def provision(url: str):
    keys = {"SLATE_CLOUD_URL": url, "SLATE_DEVICE_TOKEN": TOKEN}
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def line(link: Link, text: str, seconds: float = 20) -> str:
    existing = next((entry for entry in reversed(link.lines) if text in entry), None)
    return existing or await link.wait_for(text, seconds)


async def receive(socket, kind: str):
    async with asyncio.timeout(20):
        while True:
            frame = await socket.recv()
            if isinstance(frame, str):
                document = json.loads(frame)
                if document["type"] == kind:
                    return document


async def offline(realtime: bool) -> None:
    accepted = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def handler(socket):
        assert socket.request.headers["Authorization"] == f"Bearer {TOKEN}"
        if not accepted.done():
            accepted.set_result(socket)
        await release.wait()

    async with serve(handler, "127.0.0.1", 0, compression=None) as server:
        port = server.sockets[0].getsockname()[1]
        with provision(f"http://10.0.2.2:{port}"):
            async with breadboard(realtime=realtime) as bench:
                try:
                    async with asyncio.timeout(30):
                        socket = await accepted
                    hello = await receive(socket, "hello")
                    assert hello == {
                        "type": "hello",
                        "mcu": "esp32-wroom-32",
                        "rate": 16000,
                    }
                    print(await line(bench.link, "slate.net: up"))
                    print(await line(bench.link, "slate.cloud: connected"))
                    command = {
                        "type": "command",
                        "request_id": "a" * 32,
                        "operation": "set_orb",
                        "arguments": {"color": "#0000ff", "radius": 12},
                    }
                    await socket.send(json.dumps(command))
                    receipt = await receive(socket, "receipt")
                    assert receipt["request_id"] == command["request_id"]
                    assert receipt["operation"] == "set_orb"
                    assert receipt["state"] == 0
                    assert receipt["color"] == "#0000ff" and receipt["radius"] == 12
                    await bench.sleep(0.1)
                    assert bench.oled.frames[-1].pixels[63 * 128 + 63] == 0x001F
                    revision = receipt["revision"]
                    command["arguments"]["radius"] = 99
                    await socket.send(json.dumps(command))
                    assert (await receive(socket, "receipt"))[
                        "error"
                    ] == "invalid command"
                    command.update(operation="show_text", arguments={"text": "A"})
                    await socket.send(json.dumps(command))
                    receipt = await receive(socket, "receipt")
                    assert receipt["text"] == "A" and receipt["revision"] > revision
                    await bench.sleep(0.1)
                    pixels = bench.oled.frames[-1].pixels
                    for column, bits in enumerate((0x7C, 0x12, 0x11, 0x12, 0x7C)):
                        for row in range(8):
                            expected = 0xFFFF if bits & (1 << row) else 0
                            assert pixels[(96 + row) * 128 + column + 1] == expected
                    bench.link.type("1")
                    await receive(socket, "start")
                    await socket.send(
                        json.dumps({"type": "turn", "turn_id": "fixture"})
                    )
                    bench.feed(mic_slot_pcm(tone(440, 12000, 1.0, 16000), bench.slot))
                    frames = []
                    async with asyncio.timeout(20):
                        while not any(
                            any(struct.unpack(f"<{len(f) // 2}h", f)) for f in frames
                        ):
                            frame = await socket.recv()
                            if isinstance(frame, bytes):
                                assert 640 <= len(frame) <= 1280
                                frames.append(frame)
                    bench.link.type("3")
                    await receive(socket, "end")
                    await socket.send(
                        json.dumps(
                            {
                                "type": "transcript",
                                "turn_id": "fixture",
                                "text": "Tone captured",
                                "final": True,
                            }
                        )
                    )
                    await line(bench.link, "slate.transcript: Tone captured")
                    await socket.send(
                        json.dumps(
                            {
                                "type": "reply",
                                "turn_id": "fixture",
                                "text": "Hello Slate",
                                "final": False,
                            }
                        )
                    )
                    await line(bench.link, "slate.state: 4")
                    await socket.send(bytes(960))
                    await socket.send(
                        json.dumps(
                            {
                                "type": "reply",
                                "turn_id": "fixture",
                                "text": "Hello Slate",
                                "final": True,
                            }
                        )
                    )
                    print(await line(bench.link, "slate.reply: Hello Slate"))
                    print(await line(bench.link, "slate.reply.audio: 960 bytes"))
                    assert bench.link.state() in (0, 4)
                    await bench.sleep(0.1)
                    assert bench.link.state() == 0
                    bench.link.type("1")
                    await receive(socket, "start")
                    bench.link.type("0")
                    await receive(socket, "cancel")
                    bench.link.type("ac")
                    await receive(socket, "live")
                    print(await line(bench.link, "slate.live: requested"))
                    await socket.send(
                        json.dumps(
                            {
                                "type": "live",
                                "state": "started",
                                "call_id": "live-fixture",
                            }
                        )
                    )
                    print(await line(bench.link, "slate.live: started live-fixture"))
                    bench.feed(mic_slot_pcm(tone(440, 12000, 0.3, 16000), bench.slot))
                    captured = 0
                    async with asyncio.timeout(20):
                        while captured < 5:
                            frame = await socket.recv()
                            assert isinstance(frame, bytes), (
                                f"unexpected live control: {frame!r}"
                            )
                            captured += 1
                    speaker = struct.pack(
                        "<480h", *(index - 240 for index in range(480))
                    )
                    await socket.send(speaker)
                    async with asyncio.timeout(10):
                        assert await bench.link.speaker.get() == speaker
                    for kind, text in (("heard", "Hello live"), ("said", "Hello back")):
                        await socket.send(
                            json.dumps(
                                {"type": kind, "call_id": "live-fixture", "text": text}
                            )
                        )
                        print(await line(bench.link, f"slate.live.{kind}: {text}"))
                    await socket.send(
                        json.dumps(
                            {
                                "type": "tool",
                                "tool": "fixture_tool",
                                "turn_id": "handoff-fixture",
                            }
                        )
                    )
                    await line(bench.link, "slate.cloud.tool: fixture_tool")
                    await socket.send(
                        json.dumps({"type": "approval", "turn_id": "handoff-fixture"})
                    )
                    await line(bench.link, "slate.cloud.approval: requested")
                    await socket.send(
                        json.dumps(
                            {
                                "type": "error",
                                "turn_id": "handoff-fixture",
                                "message": "fixture live error",
                            }
                        )
                    )
                    await line(bench.link, "slate.cloud.error: fixture live error")
                    bench.link.type("3")
                    await line(bench.link, "slate.state: 3")
                    async with asyncio.timeout(10):
                        for _ in range(3):
                            assert isinstance(await socket.recv(), bytes)
                    await bench.sleep(1.1)
                    assert bench.link.state() == 3
                    bench.link.type("c")
                    await receive(socket, "hangup")
                    print(await line(bench.link, "slate.live: hangup"))
                    await socket.send(
                        json.dumps(
                            {
                                "type": "live",
                                "state": "ended",
                                "call_id": "live-fixture",
                                "seconds": 1.25,
                            }
                        )
                    )
                    print(await line(bench.link, "slate.live: ended 1.25s"))
                    await bench.sleep(0.1)
                    assert bench.link.state() == 0
                    bench.link.type("1")
                    await receive(socket, "start")
                    bench.link.type("c")
                    await receive(socket, "cancel")
                    await receive(socket, "live")
                    await socket.send(
                        json.dumps({"type": "cancelled", "turn_id": "previous-turn"})
                    )
                    await socket.send(
                        json.dumps(
                            {
                                "type": "live",
                                "state": "started",
                                "call_id": "idle-fixture",
                            }
                        )
                    )
                    await line(bench.link, "slate.live: started idle-fixture")
                    await bench.sleep(0.1)
                    assert bench.link.state() == 1
                    bench.link.type("0")
                    await receive(socket, "hangup")
                    await socket.send(
                        json.dumps(
                            {
                                "type": "live",
                                "state": "ended",
                                "call_id": "idle-fixture",
                                "seconds": 0.5,
                                "error": "fixture ended",
                            }
                        )
                    )
                    print(
                        await line(
                            bench.link, "slate.live: ended 0.50s error=fixture ended"
                        )
                    )
                    await bench.sleep(0.1)
                    assert bench.link.state() == 0
                    bench.link.type("x")
                    perf = await bench.link.wait_for("slate.perf:", seconds=5)
                    print(perf)
                    assert not check("cloud", parse(perf), factors())
                    bench.link.type("c")
                    await receive(socket, "live")
                    await socket.send(
                        json.dumps(
                            {
                                "type": "live",
                                "state": "started",
                                "call_id": "disconnect-fixture",
                            }
                        )
                    )
                    await line(bench.link, "slate.live: started disconnect-fixture")
                    await socket.close()
                    print(await line(bench.link, "slate.live: ended disconnected"))
                    await bench.sleep(0.1)
                    assert bench.link.state() == 0
                    assert not any(TOKEN in entry for entry in bench.link.lines)
                    print(
                        f"slate.cloud.check: realtime={realtime} hello, bearer, "
                        "command receipts, SPI pixels, nonzero mic PCM, start/end, "
                        "reply audio, cancellation, live call, continuous mic "
                        "and speaker tap passed"
                    )
                finally:
                    release.set()


async def tls(url: str) -> None:
    with provision(url):
        async with breadboard(realtime=True) as bench:
            print(await line(bench.link, "slate.net: up"))
            print(await line(bench.link, "slate.cloud: TLS verified", seconds=90))
            perf = await bench.link.wait_for("slate.perf:", seconds=5)
            print(perf)
            failures = check("tls", parse(perf), factors())
            timing = [
                failure for failure in failures if failure.startswith("frame takes")
            ]
            for failure in timing:
                print(f"slate.cloud.check: TLS startup timing: {failure}")
            assert not [failure for failure in failures if failure not in timing]
            print("slate.cloud.check: verified TLS and memory/stack limits")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tls-url")
    parser.add_argument("--realtime", action="store_true")
    args = parser.parse_args()
    build_image()
    asyncio.run(tls(args.tls_url) if args.tls_url else offline(args.realtime))


if __name__ == "__main__":
    main()
