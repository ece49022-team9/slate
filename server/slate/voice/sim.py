import asyncio
import logging
from array import array
from contextlib import suppress

from slate.breadboard import Breadboard

logger = logging.getLogger("slate.voice.sim")
SPEAKER_RATE = 24000


class Echo:
    def __init__(self, bench: Breadboard, gain: float):
        self.bench = bench
        self.gain = gain
        self.samples = 0
        self.output = 0
        self.last = -1_000_000_000
        self.seconds = 0.0

    def push(self, pcm: bytes) -> None:
        mono = array("h", pcm)
        end = self.samples + len(mono)
        stereo = array("h")
        while self.output * SPEAKER_RATE // self.bench.rate < end:
            index = self.output * SPEAKER_RATE // self.bench.rate - self.samples
            level = round(mono[index] * self.gain)
            stereo.extend((level, level))
            self.output += 1
        heard = stereo.tobytes()
        if self.bench.now - self.last > 200_000_000:
            heard = bytes(self.bench.rate * 4 * 40 // 1000) + heard
        self.last = self.bench.now
        self.samples = end
        self.bench.feed_echo(heard)
        self.seconds += len(mono) / SPEAKER_RATE


class SimCall:
    def __init__(self, bench: Breadboard, *, live: bool, echo: float):
        self.bench = bench
        self.live = live
        self.echo = Echo(bench, echo) if echo else None
        self.speakers: set[asyncio.Queue[bytes]] = set()
        self.offset = len(bench.link.lines)
        self.closed = False
        self.talking = False
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        while not self.bench.link.speaker.empty():
            self.bench.link.speaker.get_nowait()
        self.tasks = [
            asyncio.create_task(self.stream()),
            asyncio.create_task(self.drain_microphone()),
        ]
        self.bench.link.type("ac" if self.live else "a")

    async def drain_microphone(self) -> None:
        while True:
            await self.bench.link.audio.get()

    async def stream(self) -> None:
        try:
            while True:
                pcm = await self.bench.link.speaker.get()
                if self.echo:
                    self.echo.push(pcm)
                for queue in self.speakers:
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(pcm)
        except Exception:
            logger.exception("slate.sim: speaker forwarding failed")
            raise

    def talk(self) -> None:
        if self.live:
            raise ValueError("Always listening calls have no push to talk button")
        if self.closed:
            raise ValueError("The call has ended")
        if self.talking:
            raise ValueError("Release Talk before pressing it again")
        self.talking = True
        self.bench.link.type("1")

    def release(self) -> None:
        if self.talking:
            self.bench.link.type("3")
            self.talking = False

    async def status(self) -> dict:
        words = []
        turns = []
        handoffs = []
        error = None
        call_id = None
        ready = not self.live
        ended = self.closed
        seconds = None
        for line in self.bench.link.lines[self.offset :]:
            if line.startswith("slate.live: started "):
                ready = True
                call_id = line.removeprefix("slate.live: started ")
            elif line.startswith("slate.live: ended "):
                ready = False
                ended = True
                seconds = line.removeprefix("slate.live: ended ")
                if "error=" in seconds:
                    error = seconds.split("error=", 1)[1]
                elif seconds == "disconnected":
                    error = "Cloud disconnected"
            elif line.startswith(("slate.live.heard: ", "slate.live.said: ")):
                role = "user" if line.startswith("slate.live.heard:") else "assistant"
                text = line.split(": ", 1)[1]
                if words and words[-1]["role"] == role:
                    words[-1]["text"] += text
                else:
                    words.append({"role": role, "text": text})
            elif line.startswith("slate.transcript: "):
                turns.append({"heard": line.split(": ", 1)[1], "said": ""})
            elif line.startswith("slate.reply: "):
                if not turns:
                    turns.append({"heard": "", "said": ""})
                turns[-1]["said"] = line.split(": ", 1)[1]
            elif line.startswith("slate.cloud.tool: "):
                handoffs.append(line.split(": ", 1)[1])
            elif line.startswith(
                ("slate.cloud.error:", "slate.live: cloud disconnected")
            ):
                error = line
                if line.startswith("slate.live:"):
                    ended = True
        return {
            "active": not ended,
            "ready": ready,
            "live": self.live,
            "call_id": call_id,
            "seconds": seconds,
            "talking": self.talking,
            "echo": self.echo.gain if self.echo else 0,
            "echo_seconds": round(self.echo.seconds, 1) if self.echo else 0,
            "words": words[-12:],
            "turns": turns[-6:],
            "handoffs": len(handoffs),
            "tools": handoffs,
            "error": error,
        }

    async def stop(self) -> dict:
        status = await self.status()
        if not self.closed:
            self.bench.link.type("cx" if self.live and status["active"] else "0x")
            self.closed = True
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.bench.echo.clear()
        for queue in self.speakers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(b"")
        return await self.status()
