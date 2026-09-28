import asyncio
from pathlib import Path

import serial_asyncio_fast

BAUD = 921_600


BAUD = 921_600


class Link:
    """The firmware's serial port: text lines, plus audio frames marked by a zero
    byte and a little-endian length."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.lines: list[str] = []
        self.audio: asyncio.Queue[bytes] = asyncio.Queue()

    async def run(self, log: Path) -> None:
        buffer = bytearray()
        text = bytearray()
        with log.open("w") as output:
            while chunk := await self.reader.read(65536):
                buffer += chunk
                while buffer:
                    if buffer[0] == 0:
                        if len(buffer) < 3:
                            break
                        size = buffer[1] | buffer[2] << 8
                        if len(buffer) < 3 + size:
                            break
                        self.audio.put_nowait(bytes(buffer[3 : 3 + size]))
                        del buffer[: 3 + size]
                        continue
                    stops = [i for i in (buffer.find(0), buffer.find(b"\n")) if i >= 0]
                    end = min(stops, default=len(buffer))
                    text += buffer[:end]
                    del buffer[:end]
                    if buffer[:1] == b"\n":
                        del buffer[:1]
                        line = text.decode(errors="replace").rstrip("\r")
                        text.clear()
                        self.lines.append(line)
                        output.write(line + "\n")
                        output.flush()

    async def type(self, text: str) -> None:
        self.writer.write(text.encode())
        await self.writer.drain()

    async def wait_for(self, text: str, seconds: float = 10) -> str:
        start = len(self.lines)
        async with asyncio.timeout(seconds):
            while True:
                for line in self.lines[start:]:
                    if text in line:
                        return line
                start = len(self.lines)
                await asyncio.sleep(0.02)

    def state(self) -> int | None:
        states = [line for line in self.lines if line.startswith("slate.state: ")]
        return int(states[-1].split(": ")[1]) if states else None


async def open_board(port: str, log: Path) -> tuple[Link, asyncio.Task]:
    reader, writer = await serial_asyncio_fast.open_serial_connection(
        url=port, baudrate=BAUD
    )
    board = writer.transport.serial
    board.dtr = False
    board.rts = True
    await asyncio.sleep(0.15)
    board.rts = False
    link = Link(reader, writer)
    task = asyncio.create_task(link.run(log))
    async with asyncio.timeout(10):
        while link.state() is None:
            await asyncio.sleep(0.05)
    return link, task
