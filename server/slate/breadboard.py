import asyncio
import math
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from slate.board import ROOT, load
from slate.link import Link

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
QEMU_LOG = ROOT / ".local/qemu.log"
WIDTH = HEIGHT = 128
TICK_NS = 1_000_000
LEAD_MS = 200
SETTLE = 5


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
    frames: deque[Frame] = field(default_factory=lambda: deque(maxlen=300))
    count: int = 0

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
                self.count += 1


class Breadboard:
    """The parts around the ESP32, run in lockstep with QEMU. Emulated time only
    moves between ticks, and every input is sent in reply to a tick, so the same
    inputs give the same run."""

    def __init__(self, oled: Ssd1351, slot: int, rate: int, realtime: bool):
        self.oled = oled
        self.slot = slot
        self.realtime = realtime
        self.now = 0
        self.uart = bytearray()
        self.output = bytearray()
        self.audio = bytearray()
        self.credit = 0
        self.per_tick = rate * 4 * TICK_NS // 1_000_000_000
        self.lead = rate * 4 * LEAD_MS // 1000
        self.played: list[asyncio.Future] = []
        self.alarms: list[tuple[int, Callable[[], None]]] = []
        self.link = Link(self.uart.extend, SERIAL, self.timeout)
        self.levels: dict[int, int] = {}
        self.toggles: Counter[int] = Counter()
        self.spi_bytes = 0
        self.audio_bytes = 0

    async def wires(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        start = time.monotonic()
        while True:
            kind, ns, size = struct.unpack("<cQH", await reader.readexactly(11))
            payload = await reader.readexactly(size)
            if kind == b"G":
                self.levels[payload[0]] = payload[1]
                self.toggles[payload[0]] += 1
                self.oled.pin(ns, payload[0], payload[1])
            elif kind == b"S":
                self.spi_bytes += size
                self.oled.spi(ns, payload)
            elif kind == b"O":
                self.output += payload
            elif kind == b"T":
                await self.tick(ns)
                if self.realtime:
                    await asyncio.sleep(max(0, ns / 1e9 - (time.monotonic() - start)))
                writer.write(self.reply())
                await writer.drain()
            else:
                raise RuntimeError(f"slate.breadboard: unknown message {kind!r}")

    async def tick(self, ns: int) -> None:
        self.now = ns
        self.link.feed(bytes(self.output))
        self.output.clear()
        due = [alarm for alarm in self.alarms if alarm[0] <= ns]
        for alarm in due:
            self.alarms.remove(alarm)
            alarm[1]()
        for _ in range(SETTLE):
            await asyncio.sleep(0)

    def reply(self) -> bytes:
        message = b""
        if self.uart:
            message += struct.pack("<cH", b"U", len(self.uart)) + self.uart
            self.uart.clear()
        if self.audio:
            self.credit += self.per_tick
            chunk = self.audio[: self.credit]
            del self.audio[: len(chunk)]
            self.credit -= len(chunk)
            self.audio_bytes += len(chunk)
            message += struct.pack("<cH", b"A", len(chunk)) + chunk
        if not self.audio:
            self.credit = 0
            for future in self.played:
                future.set_result(None)
            self.played.clear()
        return message + b"R\0\0"

    def alarm(self, seconds: float, callback: Callable[[], None]) -> tuple:
        entry = (self.now + round(seconds * 1e9), callback)
        self.alarms.append(entry)
        return entry

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.alarm(seconds, lambda: future.done() or future.set_result(None))
        await future

    @asynccontextmanager
    async def timeout(self, seconds: float) -> AsyncIterator[None]:
        task = asyncio.current_task()
        expired = []
        entry = self.alarm(seconds, lambda: (expired.append(True), task.cancel()))
        try:
            yield
        except asyncio.CancelledError:
            if expired:
                raise TimeoutError(f"slate.breadboard: waited {seconds}s") from None
            raise
        finally:
            if entry in self.alarms:
                self.alarms.remove(entry)

    async def speak(self, samples: list[int]) -> None:
        stereo = [0, 0] * len(samples)
        stereo[self.slot :: 2] = samples
        await self.play(struct.pack(f"<{len(stereo)}h", *stereo))

    async def play(self, stereo: bytes) -> None:
        self.feed(stereo)
        future = asyncio.get_running_loop().create_future()
        self.played.append(future)
        await future

    def feed(self, stereo: bytes) -> None:
        if not self.audio:
            self.credit = self.lead
        self.audio += stereo


def tone(hz: float, amplitude: int, seconds: float, rate: int) -> list[int]:
    return [
        round(amplitude * math.sin(2 * math.pi * hz * i / rate))
        for i in range(int(seconds * rate))
    ]


def build_image() -> Path:
    subprocess.run(["make", "firmware"], cwd=ROOT, check=True)
    IMAGE.parent.mkdir(exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            "esp32",
            "merge-bin",
            "--pad-to-size",
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
async def breadboard(realtime: bool = False) -> AsyncIterator[Breadboard]:
    if not QEMU.exists():
        raise RuntimeError(f"slate.breadboard: {QEMU} is missing; run make sim-setup")
    board, _ = load()
    mic = board["device"]["mic"]
    bench = Breadboard(
        Ssd1351(board["device"]["oled"]["pins"]),
        ["left", "right"].index(mic["slot"]),
        mic["sample_hz"],
        realtime,
    )
    with tempfile.TemporaryDirectory(prefix="slate-board-", dir="/tmp") as directory:
        path = Path(directory) / "wires.sock"
        tasks: list[asyncio.Task] = []

        async def connection(reader, writer) -> None:
            tasks.append(asyncio.current_task())
            try:
                await bench.wires(reader, writer)
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            finally:
                writer.close()

        server = await asyncio.start_unix_server(connection, path)
        booted = asyncio.create_task(bench.link.wait_for("slate.state: 0", 30))
        process = await asyncio.create_subprocess_exec(
            str(QEMU),
            "-machine",
            "esp32",
            "-nographic",
            "-monitor",
            "none",
            "-icount",
            "shift=2,sleep=off",
            "-seed",
            "1",
            "-drive",
            f"file={IMAGE},if=mtd,format=raw",
            "-chardev",
            f"socket,id=slate,path={path}",
            "-serial",
            "null",
            "-global",
            f"driver=esp32.i2s,property=sample-rate,value={mic['sample_hz']}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=QEMU_LOG.open("w"),
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(60):
                await booted
            yield bench
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
            for task in tasks:
                task.cancel()
            server.close()
