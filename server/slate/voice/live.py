import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from openai import AsyncOpenAI

from slate.voice.audio import SAMPLE_RATE
from slate.voice.timing import Timeline

logger = logging.getLogger("slate.voice.live")
INSTRUCTIONS = Path(__file__).with_name("live.md").read_text()
BACKEND_CONTEXT = (
    "Slate's voice model will say your answer out loud. The transcript below "
    "comes from speech recognition, so it may have mistakes. Answer the user's "
    "most recent request.\n\n"
)
FAILED_REPLY = "I couldn't finish that. Please try again."
TRANSCRIPT_QUIET_NS = 250_000_000
TRANSCRIPT_LIMIT_NS = 1_000_000_000
RECONNECTS = 5
LAST_WORD_MS = 500


@dataclass
class Words:
    role: str
    start_ms: int
    end_ms: int
    text: str


def lines(words: list[Words]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for part in sorted(words, key=lambda part: part.start_ms):
        if merged and merged[-1][0] == part.role:
            merged[-1] = (part.role, merged[-1][1] + part.text)
        else:
            merged.append((part.role, part.text))
    return [(role, text.strip()) for role, text in merged if text.strip()]


def transcript(words: list[Words]) -> str:
    return "\n".join(f"{role}: {text}" for role, text in lines(words))


def history(words: list[Words]) -> list[dict]:
    """The conversation so far, to seed a session that replaces a dropped one."""
    return [
        {"role": "user", "content": [{"type": "input_text", "text": text}]}
        if role == "User"
        else {"role": "assistant", "content": [{"type": "output_text", "text": text}]}
        for role, text in lines(words)
    ]


class LiveCall:
    """One always-listening conversation. Device microphone PCM goes to GPT-Live,
    Slate's voice comes back through `speak`, and each handoff runs through
    `delegate`, which returns the reply and details to keep in the report."""

    def __init__(
        self,
        *,
        delegate: Callable[[str], Awaitable[tuple[str, dict]]],
        speak: Callable[[bytes], None],
        send: Callable[[dict], Awaitable[None]],
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.id = uuid4().hex
        self.delegate = delegate
        self.speak = speak
        self.send_event = send
        self.client = client
        self.connection = None
        self.mic: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.started = asyncio.Event()
        self.announced = False
        self.restarting = False
        self.timing = Timeline()
        self.words: list[Words] = []
        self.heard_ns = 0
        self.sent = 0
        self.delegations: list[dict] = []
        self.order = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.finished_seconds = 0.0
        self.session_seconds = 0.0

    @property
    def seconds(self) -> float:
        return self.finished_seconds + self.session_seconds

    def config(self) -> dict:
        session: dict[str, Any] = {
            "model": "gpt-live-1",
            "instructions": INSTRUCTIONS,
            "audio": {
                "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                "output": {"voice": "marin"},
            },
            "delegation": {"type": "client"},
        }
        if seed := history(self.words):
            session["input"] = seed
        return session

    async def publish(self, kind: str, **fields: Any) -> None:
        await self.send_event({"type": kind, "call_id": self.id, **fields})

    def push(self, pcm: bytes) -> None:
        self.mic.put_nowait(pcm)

    def hang_up(self) -> None:
        self.mic.put_nowait(None)

    async def run(self) -> None:
        self.timing.mark("connect_requested")
        client = self.client or AsyncOpenAI()
        try:
            async with client.live.connect(
                max_retries=RECONNECTS, on_reconnecting=self.reconnecting
            ) as connection:
                self.connection = connection
                await connection.session.start(session=self.config())
                async with asyncio.TaskGroup() as group:
                    group.create_task(self.receive())
                    group.create_task(self.send())
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.client is None:
                await client.close()

    def reconnecting(self, event) -> None:
        logger.warning(
            "GPT-Live connection for call %s dropped (code %s); reconnect %s of %s",
            self.id,
            event.close_code,
            event.attempt,
            event.max_attempts,
        )
        self.started.clear()
        if self.restarting:
            return
        self.restarting = True
        self.finished_seconds += self.session_seconds
        self.session_seconds = 0.0
        self.spawn(self.connection.session.start(session=self.config()))

    def spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def send(self) -> None:
        while (chunk := await self.mic.get()) is not None:
            if self.started.is_set():
                self.timing.mark("mic_first_sent")
                await self.connection.session.input_audio.append(
                    audio=base64.b64encode(chunk).decode()
                )
        self.timing.mark("hangup")
        await self.connection.session.close()

    async def receive(self) -> None:
        async for event in self.connection:
            kind = event.type
            if kind == "session.started":
                logger.info(
                    "GPT-Live session %s started for call %s", event.session.id, self.id
                )
                self.timing.mark("live_started")
                self.restarting = False
                self.started.set()
                if not self.announced:
                    self.announced = True
                    await self.publish("live", state="started")
            elif kind == "session.output_audio.delta":
                self.timing.mark("output_first_audio")
                self.speak(base64.b64decode(event.delta))
            elif kind == "session.input_transcript.delta":
                self.heard_ns = time.monotonic_ns()
                self.words.append(
                    Words("User", event.start_ms, event.end_ms, event.delta)
                )
                await self.publish("heard", text=event.delta)
            elif kind == "session.output_transcript.delta":
                self.words.append(
                    Words("Slate", event.start_ms, event.end_ms, event.delta)
                )
                await self.publish("said", text=event.delta)
            elif kind == "session.delegation.created":
                if event.delegation.target == "client":
                    self.spawn(self.answer(event.delegation.id, event.offset_ms))
            elif kind == "session.usage.updated":
                self.session_seconds = event.usage.seconds
            elif kind == "error":
                logger.error(
                    "GPT-Live error for call %s event %s: %s %s",
                    self.id,
                    event.client_event_id,
                    event.error.code,
                    event.error.message,
                )
                if event.client_event_id is None:
                    raise RuntimeError(f"slate.live: {event.error.code}")
            elif kind == "session.closed":
                self.session_seconds = event.usage.seconds
                self.timing.mark("live_closed")
                if event.reason != "close_requested":
                    raise RuntimeError(f"slate.live: session closed: {event.reason}")
                return
            elif kind == "info":
                logger.info("GPT-Live info %s: %s", event.code, event.message)
        raise RuntimeError("slate.live: GPT-Live disconnected before session.closed")

    async def settle_transcript(self) -> None:
        deadline = time.monotonic_ns() + TRANSCRIPT_LIMIT_NS
        while (now := time.monotonic_ns()) < deadline:
            quiet = now - self.heard_ns
            if quiet >= TRANSCRIPT_QUIET_NS:
                return
            await asyncio.sleep((TRANSCRIPT_QUIET_NS - quiet) / 1e9)

    async def answer(self, delegation_id: str, offset_ms: int) -> None:
        async with self.order:
            record: dict[str, Any] = {
                "id": delegation_id,
                "offset_ms": offset_ms,
                "created_ns": time.monotonic_ns(),
            }
            self.delegations.append(record)
            try:
                await self.settle_transcript()
                request = transcript(self.unsent(offset_ms))
                record["request"] = request
                if not request:
                    raise ValueError("No transcript arrived before this handoff")
                record["agent_requested_ns"] = time.monotonic_ns()
                reply, details = await self.delegate(BACKEND_CONTEXT + request)
                record["agent_completed_ns"] = time.monotonic_ns()
                record.update(details, reply=reply)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("Handoff %s failed in call %s", delegation_id, self.id)
                record["error"] = type(error).__name__
                reply = FAILED_REPLY
            record["commentary_sent_ns"] = time.monotonic_ns()
            await self.connection.session.commentary.append(
                delegation_id=delegation_id, content=reply
            )

    def unsent(self, offset_ms: int) -> list[Words]:
        """Words since the previous handoff and before this one. The user's last
        word can start just after the handoff, since GPT-Live often hands off as
        it hears it; Slate's acknowledgment of the handoff waits for the next."""
        fresh = self.words[self.sent :]
        kept = [
            part
            for part in fresh
            if part.start_ms < offset_ms + (LAST_WORD_MS if part.role == "User" else 0)
        ]
        later = [part for part in fresh if part not in kept]
        self.words[self.sent :] = kept + later
        self.sent += len(kept)
        return kept

    async def tell(self, text: str) -> None:
        """Gives GPT-Live a result nobody asked for in this call, such as a
        background task that just finished."""
        await self.connection.session.commentary.append(content=text)

    def report(self) -> dict:
        return {
            "call_id": self.id,
            "seconds": self.seconds,
            "server": self.timing.snapshot(),
            "delegations": self.delegations,
            "words": [asdict(part) for part in self.words],
        }
