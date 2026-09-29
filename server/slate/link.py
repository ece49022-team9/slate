import asyncio
from collections.abc import Callable
from pathlib import Path

import serial_asyncio_fast

BAUD = 921_600


class Link:
    """The firmware's serial port: text lines, plus audio frames marked by a zero
    byte and a little-endian length."""

    def __init__(
        self,
        send: Callable[[bytes], object],
        log: Path,
        timeout: Callable = asyncio.timeout,
    ):
        self.send = send
        self.timeout = timeout
        self.lines: list[str] = []
        self.audio: asyncio.Queue[bytes] = asyncio.Queue()
        self.buffer = bytearray()
        self.text = bytearray()
        self.waiters: list[tuple[str, asyncio.Future]] = []
        log.parent.mkdir(exist_ok=True)
        self.log = log.open("w")

    def feed(self, data: bytes) -> None:
        buffer = self.buffer
        buffer += data
        while buffer:
            if buffer[0] == 0:
                if len(buffer) < 3:
                    return
                size = buffer[1] | buffer[2] << 8
                if len(buffer) < 3 + size:
                    return
                self.audio.put_nowait(bytes(buffer[3 : 3 + size]))
                del buffer[: 3 + size]
                continue
            stops = [i for i in (buffer.find(0), buffer.find(b"\n")) if i >= 0]
            end = min(stops, default=len(buffer))
            self.text += buffer[:end]
            del buffer[:end]
            if buffer[:1] == b"\n":
                del buffer[:1]
                self.line(self.text.decode(errors="replace").rstrip("\r"))
                self.text.clear()

    def line(self, line: str) -> None:
        self.lines.append(line)
        self.log.write(line + "\n")
        self.log.flush()
        for waiter in [waiter for waiter in self.waiters if waiter[0] in line]:
            self.waiters.remove(waiter)
            if not waiter[1].done():
                waiter[1].set_result(line)

    def type(self, text: str) -> None:
        self.send(text.encode())

    async def wait_for(self, text: str, seconds: float = 10) -> str:
        waiter = (text, asyncio.get_running_loop().create_future())
        self.waiters.append(waiter)
        try:
            async with self.timeout(seconds):
                return await waiter[1]
        finally:
            if waiter in self.waiters:
                self.waiters.remove(waiter)

    def state(self) -> int | None:
        states = [line for line in self.lines if line.startswith("slate.state: ")]
        return int(states[-1].split(": ")[1]) if states else None


async def open_board(port: str, log: Path) -> tuple[Link, asyncio.Task]:
    reader, writer = await serial_asyncio_fast.open_serial_connection(
        url=port, baudrate=BAUD
    )
    link = Link(writer.write, log)

    async def read() -> None:
        try:
            while chunk := await reader.read(65536):
                link.feed(chunk)
        finally:
            writer.close()

    task = asyncio.create_task(read())
    booted = asyncio.create_task(link.wait_for("slate.state: 0"))
    board = writer.transport.serial
    board.dtr = False
    board.rts = True
    await asyncio.sleep(0.15)
    board.rts = False
    await booted
    return link, task
