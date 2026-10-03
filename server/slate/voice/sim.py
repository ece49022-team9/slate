import asyncio
import logging
import time
from array import array
from collections.abc import AsyncIterator, Coroutine
from contextlib import aclosing
from typing import Any

from livekit import rtc

from slate.breadboard import Breadboard
from slate.voice.audio import SAMPLE_RATE
from slate.voice.device import CascadeCall, LiveCall
from slate.voice.firmware import listen

logger = logging.getLogger("slate.voice.sim")
ECHO_DELAY_MS = 40
ECHO_GAP_S = 0.2


class Echo:
    """Slate's reply as the device's own speaker would put it into the mics:
    quieter, a little late, and heard by both mics."""

    def __init__(self, bench: Breadboard, rate: int, gain: float) -> None:
        if not 0 < gain <= 1:
            raise ValueError("Echo gain must be above 0 and at most 1")
        self.bench = bench
        self.rate = rate
        self.gain = gain
        self.resampler = rtc.AudioResampler(SAMPLE_RATE, rate, num_channels=1)
        self.delay = bytes(rate * 4 * ECHO_DELAY_MS // 1000)
        self.last = 0.0
        self.seconds = 0.0

    def push(self, pcm: bytes) -> None:
        frames = self.resampler.push(rtc.AudioFrame(pcm, SAMPLE_RATE, 1, len(pcm) // 2))
        mono = array("h", b"".join(frame.data.tobytes() for frame in frames))
        if not mono:
            return
        stereo = array("h", bytes(len(mono) * 4))
        for index, sample in enumerate(mono):
            level = max(-32768, min(32767, round(sample * self.gain)))
            stereo[2 * index] = stereo[2 * index + 1] = level
        heard = stereo.tobytes()
        now = time.monotonic()
        if now - self.last > ECHO_GAP_S:
            heard = self.delay + heard
        self.last = now
        self.bench.feed_echo(heard)
        self.seconds += len(mono) / self.rate


class SimCall:
    """The simulated device on a voice call. The firmware's mic stream goes to
    Slate; Slate's reply goes to every open speaker and, with echo on, back into
    the simulated mics."""

    def __init__(
        self, bench: Breadboard, api_url: str, rate: int, *, live: bool, echo: float
    ) -> None:
        self.bench = bench
        self.api_url = api_url
        self.live = live
        self.channel = ("left", "right")[bench.slot]
        self.echo = Echo(bench, rate, echo) if echo else None
        self.speakers: set[asyncio.Queue[bytes]] = set()
        kind = LiveCall if live else CascadeCall
        self.call = kind(api_url, rate, on_reply=self.reply)
        self.tasks: set[asyncio.Task] = set()
        self.released: asyncio.Event | None = None
        self.turns: list[dict[str, str]] = []
        self.error: str | None = None
        self.closed = False

    def spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.finished)

    def finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()):
            logger.error("Simulated call task failed", exc_info=error)
            self.error = str(error) or type(error).__name__

    def reply(self, pcm: bytes) -> None:
        for speaker in self.speakers:
            speaker.put_nowait(pcm)
        if self.echo:
            self.echo.push(pcm)

    async def start(self) -> None:
        await self.call.__aenter__()
        if self.live:
            self.spawn(self.stream())

    async def microphone(self) -> AsyncIterator[bytes]:
        async with aclosing(listen(self.bench.link, self.channel)) as chunks:
            async for chunk in chunks:
                yield chunk
                if self.released and self.released.is_set():
                    return

    async def stream(self) -> None:
        async for chunk in self.microphone():
            await self.call.send(chunk)

    def talk(self) -> None:
        if self.live:
            raise ValueError("Always-listening calls have no push-to-talk button")
        if self.released is not None:
            raise ValueError("Release the button before pressing it again")
        self.released = asyncio.Event()
        self.spawn(self.turn())

    def release(self) -> None:
        if self.released:
            self.released.set()

    async def turn(self) -> None:
        try:
            result = await self.call.turn(self.microphone())
            self.turns.append({"heard": result.transcript, "said": result.reply})
        finally:
            self.released = None

    async def status(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "active": not self.closed,
            "live": self.live,
            "echo": self.echo.gain if self.echo else 0,
            "echo_seconds": round(self.echo.seconds, 1) if self.echo else 0,
            "talking": self.released is not None,
            "error": self.error,
        }
        if self.live:
            report = await self.call.report()
            state["ready"] = report["ready"]
            state["handoffs"] = len(report["delegations"])
            state["words"] = [
                {"role": part["role"], "text": part["text"]}
                for part in report["words"][-12:]
            ]
        else:
            state["turns"] = self.turns[-6:]
        return state

    async def stop(self) -> dict[str, Any]:
        self.closed = True
        for speaker in self.speakers:
            speaker.put_nowait(b"")
        usage = None
        try:
            if self.live and self.call.session:
                async with asyncio.timeout(30):
                    usage = (await self.call.finish())["usage"]
        except Exception:
            logger.exception("GPT-Live did not close cleanly; billed seconds unknown")
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.call.close()
        return {"usage": usage}
