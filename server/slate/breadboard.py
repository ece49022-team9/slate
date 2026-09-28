import asyncio
import os
import signal
import struct
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from slate.board import ROOT, load

QEMU = Path(
    os.environ.get(
        "SLATE_QEMU", Path.home() / "esp/qemu-slate/build/qemu-system-xtensa"
    )
)
BUILD = ROOT / "firmware/.pio/build/esp32dev"
BOOT_APP = (
    Path.home()
    / ".platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin"
)
IMAGE = ROOT / ".local/board-flash.bin"
SERIAL = ROOT / ".local/board-serial.log"
WIDTH = HEIGHT = 128


@dataclass
class Frame:
    ns: int
    data: bytes

    @property
    def pixels(self) -> tuple[int, ...]:
        return struct.unpack(f">{WIDTH * HEIGHT}H", self.data)


@dataclass
class Ssd1351:
    pins: dict[str, int]
    levels: dict[int, int] = field(default_factory=dict)
    command: int | None = None
    args: list[int] = field(default_factory=list)
    columns: tuple[int, int] = (0, WIDTH - 1)
    rows: tuple[int, int] = (0, HEIGHT - 1)
    offset: int = 0
    on: bool = False
    ram: bytearray = field(default_factory=lambda: bytearray(WIDTH * HEIGHT * 2))
    frames: list[Frame] = field(default_factory=list)

    def level(self, name: str) -> int:
        return self.levels.get(self.pins[name], 0)

    def pin(self, ns: int, gpio: int, level: int) -> None:
        self.levels[gpio] = level
        if gpio == self.pins["reset"] and not level:
            self.on = False
            self.command = None

    def spi(self, ns: int, data: bytes) -> None:
        if self.level("cs") or not self.level("reset"):
            return
        if not self.level("dc"):
            for byte in data:
                self.start(byte)
        elif self.command == 0x5C:
            self.write(ns, data)
        else:
            self.args.extend(data)
            self.finish()

    def start(self, command: int) -> None:
        self.command = command
        self.args = []
        if command == 0xAF:
            self.on = True
        elif command == 0xAE:
            self.on = False

    def finish(self) -> None:
        if len(self.args) != 2 or self.command not in (0x15, 0x75):
            return
        if self.command == 0x15:
            self.columns = (self.args[0], self.args[1])
        else:
            self.rows = (self.args[0], self.args[1])
        self.offset = 0

    def write(self, ns: int, data: bytes) -> None:
        row_bytes = (self.columns[1] - self.columns[0] + 1) * 2
        window = row_bytes * (self.rows[1] - self.rows[0] + 1)
        while data:
            row, column = divmod(self.offset, row_bytes)
            take = min(len(data), row_bytes - column)
            start = ((self.rows[0] + row) * WIDTH + self.columns[0]) * 2 + column
            self.ram[start : start + take] = data[:take]
            data = data[take:]
            self.offset += take
            if self.offset == window:
                self.offset = 0
                self.frames.append(Frame(ns, bytes(self.ram)))


@dataclass
class Breadboard:
    oled: Ssd1351
    log: Path

    @property
    def serial(self) -> list[str]:
        return self.log.read_text(errors="replace").splitlines()

    async def wires(self, reader: asyncio.StreamReader) -> None:
        while True:
            kind, ns, size = struct.unpack("<cQH", await reader.readexactly(11))
            payload = await reader.readexactly(size)
            if kind == b"G":
                self.oled.pin(ns, payload[0], payload[1])
            elif kind == b"S":
                self.oled.spi(ns, payload)
            else:
                raise RuntimeError(f"slate.breadboard: unknown message {kind!r}")

    def state(self) -> int | None:
        states = [line for line in self.serial if line.startswith("slate.state: ")]
        return int(states[-1].split(": ")[1]) if states else None


def build_image() -> Path:
    subprocess.run(["make", "firmware-hardware"], cwd=ROOT, check=True)
    IMAGE.parent.mkdir(exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            "esp32",
            "merge-bin",
            "--fill-flash-size",
            "4MB",
            "-o",
            str(IMAGE),
            "0x1000",
            str(BUILD / "bootloader.bin"),
            "0x8000",
            str(BUILD / "partitions.bin"),
            "0xe000",
            str(BOOT_APP),
            "0x10000",
            str(BUILD / "firmware.bin"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return IMAGE


@asynccontextmanager
async def breadboard() -> AsyncIterator[Breadboard]:
    if not QEMU.exists():
        raise RuntimeError(f"slate.breadboard: {QEMU} is missing; run make sim-setup")
    board, _ = load()
    with tempfile.TemporaryDirectory(prefix="slate-board-", dir="/tmp") as directory:
        path = Path(directory) / "wires.sock"
        SERIAL.parent.mkdir(exist_ok=True)
        SERIAL.write_text("")
        bench = Breadboard(Ssd1351(board["device"]["oled"]["pins"]), SERIAL)
        connected = asyncio.Event()
        tasks: list[asyncio.Task] = []

        async def accept(reader, writer) -> None:
            connected.set()
            tasks.append(asyncio.current_task())
            await bench.wires(reader)

        server = await asyncio.start_unix_server(accept, path)
        process = await asyncio.create_subprocess_exec(
            str(QEMU),
            "-machine",
            "esp32",
            "-nographic",
            "-monitor",
            "none",
            "-drive",
            f"file={IMAGE},if=mtd,format=raw",
            "-chardev",
            f"socket,id=slate,path={path}",
            "-serial",
            f"file:{SERIAL}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(10):
                await connected.wait()
            yield bench
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
            for task in tasks:
                task.cancel()
            server.close()
