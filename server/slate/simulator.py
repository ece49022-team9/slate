import asyncio
import os
import signal
import struct
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRAME_SAMPLES = 320
DISPLAY_BYTES = 12 + 128 * 128 * 2


class Firmware:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.lock = asyncio.Lock()

    async def exchange(self, command: int, payload: bytes = b"") -> bytes:
        if len(payload) > FRAME_SAMPLES * 4:
            raise ValueError("slate.firmware: audio packet exceeds 20 ms")
        async with self.lock, asyncio.timeout(10):
            self.writer.write(struct.pack("<BH", command, len(payload)) + payload)
            await self.writer.drain()
            status, size = struct.unpack("<BH", await self.reader.readexactly(3))
            limit = DISPLAY_BYTES if command == 5 else FRAME_SAMPLES * 2
            if size > limit or size % 2:
                raise RuntimeError("slate.firmware: invalid audio response length")
            audio = await self.reader.readexactly(size)
            if status:
                raise RuntimeError(
                    f"slate.firmware: command {command} failed: {status}"
                )
            if command == 5 and len(audio) != DISPLAY_BYTES:
                raise RuntimeError("slate.firmware: incomplete display frame")
            return audio


@asynccontextmanager
async def qemu() -> AsyncIterator[Firmware]:
    with tempfile.TemporaryDirectory(prefix="slate-qemu-", dir="/tmp") as directory:
        socket = Path(directory) / "audio.sock"
        log_path = Path(directory) / "qemu.log"
        with log_path.open("w") as log:
            process = await asyncio.create_subprocess_exec(
                "bash",
                "scripts/esp-idf.sh",
                "qemu",
                "--qemu-extra-args",
                f"-serial unix:{socket},server=on,wait=off",
                cwd=ROOT,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            writer = None
            try:
                async with asyncio.timeout(60):
                    while "Audio bridge ready on UART1" not in log_path.read_text():
                        if any(
                            failure in log_path.read_text()
                            for failure in (
                                "assert failed:",
                                "stack overflow",
                                "Guru Meditation",
                            )
                        ):
                            raise RuntimeError("slate.firmware: firmware crashed")
                        if process.returncode is not None:
                            raise RuntimeError("slate.firmware: QEMU exited at startup")
                        await asyncio.sleep(0.1)
                reader, writer = await asyncio.open_unix_connection(socket)
                yield Firmware(reader, writer)
            except (TimeoutError, RuntimeError, asyncio.IncompleteReadError) as error:
                raise RuntimeError(
                    f"slate.firmware: simulation failed: {error}\n"
                    f"{log_path.read_text()}"
                ) from error
            finally:
                if writer is not None:
                    writer.close()
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
