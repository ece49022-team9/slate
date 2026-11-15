import asyncio
import base64
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from openai import AsyncOpenAI

from slate.voice.audio import SAMPLE_RATE
from slate.voice.timing import Timeline

logger = logging.getLogger("slate.voice.live")
INSTRUCTIONS = Path(__file__).with_name("live.md").read_text()
MODEL = "gpt-live-1"
VOICE = "marin"
BACKEND_CONTEXT = (
    "Slate's voice model will say your answer out loud. The request below comes "
    "from speech recognition, so it may have mistakes.\n\n"
)
RECONNECTS = 5
LAST_WORD_MS = 500
REPLY_GAP_MS = 800
REPLY_WORDS = 3
RESULT_CHARS = 1500
TRANSCRIPT_QUIET_NS = 250_000_000
SOUNDING_NS = 400_000_000
VERDICT_NS = 1_500_000_000
HOLD_MAX_NS = 3_000_000_000
PERSON_TALKING_NS = 1_200_000_000
VOICE_TALKING_NS = 800_000_000
VOICE_STARTED_NS = 300_000_000
SAYING_NS = 4_000_000_000
OWED_MAX_NS = 8_000_000_000
CARRY_NS = 60_000_000_000
LOUD = 400
FLOOR_RISE = 1.002
ON_THE_LINE = "They're on the line now."
BACK_ON_THE_LINE = "They're back on the line after it dropped."
PROGRESS = "Still working, not done:"
UNSAID = "Earlier update, not yet said."
GOING_ON = "What's going on now:"
WITHDRAWN = (
    "Stopped before it ran: the person kept speaking. Nothing was done for this, "
    "so hear them out first."
)
QUEUED = "Update: that's queued behind the work that's running, and starts after it."
NOT_HEARD = "Update: nothing was sent, since none of their words came through."
FAILED = "Update: the work failed before it finished."
ALLOWED = "Update: allowed, so it carries on."
DECLINED = "Update: declined, so it won't do that."
OK_DROPPED = "The OK was declined, since they said something else."
YES = re.compile(r"\b(yes|yeah|yep|sure|okay|ok|go ahead|do it|allow|fine)\b", re.I)
NO = re.compile(r"\b(no|nope|don't|do not|stop|cancel|deny|decline)\b", re.I)
ENDED = {"close_requested", "remote_hangup"}
Progress = Callable[[dict], Awaitable[None]]
Delegate = Callable[[str, Progress], Awaitable[tuple[str, dict]]]


@dataclass
class Words:
    role: str
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Reply:
    start_ms: int
    end_ms: int
    text: str
    words: int


@dataclass
class Update:
    text: str
    delegation_id: str | None
    generation: int
    again: bool = False
    record: dict | None = None
    owed_ns: int = field(default_factory=time.monotonic_ns)
    given_ns: int = 0


@dataclass
class Held:
    delegation_id: str
    offset_ms: int
    since_ns: int
    timer: asyncio.TimerHandle


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
    return [
        {"role": "user", "content": [{"type": "input_text", "text": text}]}
        if role == "User"
        else {"role": "assistant", "content": [{"type": "output_text", "text": text}]}
        for role, text in lines(words)
    ]


def has_words(text: str) -> bool:
    return bool(re.search(r"[^\W\d_]", re.sub(r"\[[^\]]*\]?", "", text)))


def rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2")[::4]
    if not samples.size:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def outcome(reply: str) -> str:
    if len(reply) <= RESULT_CHARS:
        return f"Update, finished: {reply}"
    cut = reply[:RESULT_CHARS].rsplit(" ", 1)[0]
    return f"Update, finished: {cut} (The details are on screen.)"


def ok_question(command: str) -> str:
    return f"Update, it asks your OK to run: {command}. Yes or no?"


def choice(words: str) -> str | None:
    yes, no = bool(YES.search(words)), bool(NO.search(words))
    if yes != no:
        return "once" if yes else "deny"
    return None


class Level:
    def __init__(self) -> None:
        self.floor: float | None = None

    def loud(self, pcm: bytes) -> bool:
        level = rms(pcm)
        if self.floor is None:
            self.floor = level
        self.floor = min(level, self.floor * FLOOR_RISE + 1)
        return level >= max(LOUD, 3 * self.floor)


class Core:
    def __init__(self) -> None:
        self.heard: list[Words] = []
        self.replies: list[Reply] = []
        self.handed_ms = -1

    def hear(self, part: Words) -> None:
        self.heard.append(part)

    def say(self, part: Words) -> None:
        count = len(part.text.split())
        last = self.replies[-1] if self.replies else None
        if last and part.start_ms - last.end_ms <= REPLY_GAP_MS:
            last.end_ms = part.end_ms
            last.text += part.text
            last.words += count
        else:
            self.replies.append(Reply(part.start_ms, part.end_ms, part.text, count))
        self.replies = self.replies[-8:]

    def asked(self, offset_ms: int) -> str:
        real = [
            reply
            for reply in self.replies
            if reply.words >= REPLY_WORDS and reply.end_ms <= offset_ms
        ]
        since = max(self.handed_ms, real[-1].end_ms if real else -1)
        edge = offset_ms + LAST_WORD_MS
        return "".join(
            part.text for part in self.heard if since < part.start_ms <= edge
        ).strip()

    def hand_off(self, offset_ms: int) -> str:
        words = self.asked(offset_ms)
        self.handed_ms = max(self.handed_ms, offset_ms + LAST_WORD_MS)
        self.heard = [part for part in self.heard if part.start_ms > self.handed_ms]
        return words

    def spoke_after(self, offset_ms: int) -> bool:
        edge = offset_ms + LAST_WORD_MS
        return any(part.start_ms > edge and has_words(part.text) for part in self.heard)


class LiveCall:
    def __init__(
        self,
        *,
        delegate: Delegate,
        approve: Callable[[str, str, str], Awaitable[None]],
        speak: Callable[[bytes], None],
        send: Callable[[dict], Awaitable[None]],
        background: Callable[[], int] = lambda: 0,
        rate: int = SAMPLE_RATE,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.id = uuid4().hex
        self.delegate = delegate
        self.approve = approve
        self.speak = speak
        self.send_event = send
        self.background = background
        self.rate = rate
        self.client = client
        self.owned = False
        self.session_id: str | None = None
        self.answer: str | None = None
        self.connection = None
        self.mic: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.started = asyncio.Event()
        self.announced = False
        self.restarting = False
        self.generation = 0
        self.greeted = False
        self.timing = Timeline()
        self.words: list[Words] = []
        self.core = Core()
        self.level = Level()
        self.heard_ns = 0
        self.talked_ns = 0
        self.voice_ns = 0
        self.held: Held | None = None
        self.carried: tuple[str, int] | None = None
        self.owed: list[Update] = []
        self.given: Update | None = None
        self.pacer: asyncio.TimerHandle | None = None
        self.kept: dict[str, tuple[str, str | None]] = {}
        self.summary = ""
        self.working: str | None = None
        self.queued: list[str] = []
        self.ok: tuple[str, str] | None = None
        self.delegations: list[dict] = []
        self.updates: list[dict] = []
        self.notes: list[dict] = []
        self.order = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.finished_seconds = 0.0
        self.session_seconds = 0.0

    @property
    def seconds(self) -> float:
        return self.finished_seconds + self.session_seconds

    @property
    def transport(self) -> str:
        return "webrtc" if self.session_id else "websocket"

    def openai(self) -> AsyncOpenAI:
        if self.client is None:
            self.client = AsyncOpenAI()
            self.owned = True
        return self.client

    def config(self) -> dict:
        session: dict[str, Any] = {
            "model": MODEL,
            "instructions": INSTRUCTIONS,
            "audio": {
                "format": {"type": "audio/pcm", "rate": self.rate},
                "output": {"voice": VOICE},
            },
            "delegation": {"type": "client"},
        }
        if seed := history(self.words):
            session["input"] = seed
        return session

    def media(self) -> dict:
        return {
            "model": MODEL,
            "instructions": INSTRUCTIONS,
            "audio": {"output": {"voice": VOICE}},
            "delegation": {"type": "client"},
            "client": {
                "data_channel": {
                    "allowed_client_events": [],
                    "allowed_server_events": [{"type": "session.closed"}],
                }
            },
        }

    async def open(self, offer: str) -> str:
        self.timing.mark("connect_requested")
        created = await self.openai().live.create(
            session=self.media(), transport={"type": "webrtc", "sdp": offer}
        )
        self.session_id = created.session.id
        self.answer = created.transport.sdp
        self.timing.mark("session_created")
        logger.info("GPT-Live session %s answers call %s", self.session_id, self.id)
        return self.answer

    async def publish(self, kind: str, **fields: Any) -> None:
        await self.send_event({"type": kind, "call_id": self.id, **fields})

    def push(self, pcm: bytes) -> None:
        self.heard_audio(pcm)
        self.mic.put_nowait(pcm)

    def hang_up(self) -> None:
        self.mic.put_nowait(None)

    async def run(self) -> None:
        self.timing.mark("connect_requested")
        client = self.openai()
        try:
            if self.session_id:
                async with client.live.sideband.connect(
                    session_id=self.session_id,
                    max_retries=RECONNECTS,
                    on_reconnecting=self.reattaching,
                ) as connection:
                    self.connection = connection
                    self.timing.mark("line_attached")
                    await self.announce()
                    async with asyncio.TaskGroup() as group:
                        group.create_task(self.receive())
                        group.create_task(self.wait_for_hangup())
            else:
                async with client.live.connect(
                    max_retries=RECONNECTS, on_reconnecting=self.reconnecting
                ) as connection:
                    self.connection = connection
                    await connection.session.start(session=self.config())
                    async with asyncio.TaskGroup() as group:
                        group.create_task(self.receive())
                        group.create_task(self.send())
        finally:
            if self.held:
                self.held.timer.cancel()
            if self.pacer:
                self.pacer.cancel()
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.owned:
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

    def reattaching(self, event) -> None:
        logger.warning(
            "Control line for call %s dropped (code %s); reattach %s of %s",
            self.id,
            event.close_code,
            event.attempt,
            event.max_attempts,
        )

    def spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self.tasks.add(task)
        task.add_done_callback(self.finished_task)

    def finished_task(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()):
            logger.error("Task in call %s failed", self.id, exc_info=error)

    async def announce(self) -> None:
        if not self.announced:
            self.announced = True
            await self.publish("live", state="started", transport=self.transport)

    async def send(self) -> None:
        while (chunk := await self.mic.get()) is not None:
            if self.started.is_set():
                self.timing.mark("mic_first_sent")
                await self.connection.session.input_audio.append(
                    audio=base64.b64encode(chunk).decode()
                )
        self.timing.mark("hangup")
        await self.connection.session.close()

    async def wait_for_hangup(self) -> None:
        while await self.mic.get() is not None:
            continue
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
                restarted = self.restarting
                if restarted:
                    self.generation += 1
                    self.restarting = False
                self.started.set()
                await self.announce()
                if restarted:
                    self.tell(BACK_ON_THE_LINE)
                elif not self.session_id:
                    self.greet()
            elif kind == "session.input_audio.append":
                self.heard_audio(base64.b64decode(event.audio))
                self.greet()
            elif kind == "session.output_audio.delta":
                self.timing.mark("output_first_audio")
                pcm = base64.b64decode(event.delta)
                self.voiced(pcm)
                if not self.session_id:
                    self.speak(pcm)
            elif kind == "session.input_transcript.delta":
                part = Words("User", event.start_ms, event.end_ms, event.delta)
                self.heard_ns = time.monotonic_ns()
                self.words.append(part)
                self.core.hear(part)
                await self.publish("heard", text=event.delta)
                if has_words(event.delta):
                    self.interrupted()
                    self.confide()
                if self.held:
                    self.verdict(self.held.delegation_id, self.held.offset_ms)
            elif kind == "session.output_transcript.delta":
                part = Words("Slate", event.start_ms, event.end_ms, event.delta)
                self.words.append(part)
                self.core.say(part)
                await self.publish("said", text=event.delta)
            elif kind == "session.delegation.created":
                if event.delegation.target == "client":
                    self.timing.mark("first_delegation")
                    self.verdict(event.delegation.id, event.offset_ms)
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
                if event.reason not in ENDED and not (
                    self.session_id and event.reason == "connection_lost"
                ):
                    raise RuntimeError(f"slate.live: session closed: {event.reason}")
                return
            elif kind == "info":
                logger.info("GPT-Live info %s: %s", event.code, event.message)
        raise RuntimeError("slate.live: GPT-Live disconnected before session.closed")

    def heard_audio(self, pcm: bytes) -> None:
        if self.level.loud(pcm):
            self.talked_ns = time.monotonic_ns()
            self.pace()

    def voiced(self, pcm: bytes) -> None:
        if rms(pcm) < LOUD:
            return
        now = time.monotonic_ns()
        self.voice_ns = now
        if self.given and now - self.given.given_ns > VOICE_STARTED_NS:
            self.given = None
        self.pace()

    def greet(self) -> None:
        if self.greeted:
            return
        self.greeted = True
        if not (self.talked_ns or self.heard_ns or self.voice_ns):
            self.tell(ON_THE_LINE)

    def tell(self, text: str) -> None:
        self.timing.mark("greeted")
        self.updates.append({"text": text, "given_ns": time.monotonic_ns()})
        self.spawn(
            self.connection.session.commentary.append(content=text, delegation_id=None)
        )

    def verdict(self, delegation_id: str, offset_ms: int) -> None:
        held, self.held = self.held, None
        if held:
            held.timer.cancel()
            if held.delegation_id != delegation_id:
                self.withdraw(held.delegation_id, held.offset_ms)
        if self.core.spoke_after(offset_ms):
            self.withdraw(delegation_id, offset_ms)
            return
        now = time.monotonic_ns()
        since = held.since_ns if held and held.delegation_id == delegation_id else now
        sounding = now - self.talked_ns < (VERDICT_NS if held else SOUNDING_NS)
        arriving = now - self.heard_ns < TRANSCRIPT_QUIET_NS
        if not (sounding or arriving) or now - since >= HOLD_MAX_NS:
            self.spawn(self.hand_off(delegation_id, offset_ms, since))
            return
        due = min(
            max(self.talked_ns + VERDICT_NS, self.heard_ns + TRANSCRIPT_QUIET_NS),
            since + HOLD_MAX_NS,
        )
        timer = asyncio.get_running_loop().call_later(
            max(0.05, (due - now) / 1e9), self.verdict, delegation_id, offset_ms
        )
        self.held = Held(delegation_id, offset_ms, since, timer)

    def withdraw(self, delegation_id: str, offset_ms: int) -> None:
        words = self.core.hand_off(offset_ms)
        logger.info("Withdrew handoff %s in call %s: %r", delegation_id, self.id, words)
        self.delegations.append(
            {
                "id": delegation_id,
                "offset_ms": offset_ms,
                "created_ns": time.monotonic_ns(),
                "request": words,
                "withdrawn": True,
            }
        )
        if words:
            self.carried = (self.carry(words), time.monotonic_ns())
        self.think(WITHDRAWN, delegation_id)

    def carry(self, words: str) -> str:
        carried, self.carried = self.carried, None
        if carried and time.monotonic_ns() - carried[1] < CARRY_NS:
            return f"{carried[0]} {words}".strip()
        return words

    async def hand_off(self, delegation_id: str, offset_ms: int, since: int) -> None:
        words = self.carry(self.core.hand_off(offset_ms))
        record: dict[str, Any] = {
            "id": delegation_id,
            "offset_ms": offset_ms,
            "created_ns": since,
            "handed_ns": time.monotonic_ns(),
            "request": words,
        }
        self.delegations.append(record)
        if not words:
            logger.warning("Handoff %s in call %s had no words", delegation_id, self.id)
            record["error"] = "NoWords"
            self.say(NOT_HEARD, delegation_id, record)
            return
        if self.ok is not None:
            run_id, request_id = self.ok
            self.ok = None
            answer = choice(words)
            record["approval"] = answer or "deny"
            try:
                await self.approve(run_id, request_id, answer or "deny")
            except ValueError as error:
                logger.warning("Approval in call %s went nowhere: %s", self.id, error)
            if answer:
                self.say(
                    ALLOWED if answer == "once" else DECLINED, delegation_id, record
                )
                return
            self.think(OK_DROPPED, None)
        if self.order.locked():
            self.say(QUEUED, delegation_id)
        self.queued.append(words)
        self.refresh()
        async with self.order:
            self.queued.remove(words)
            self.working = words
            self.refresh()
            record["agent_requested_ns"] = time.monotonic_ns()
            try:
                reply, details = await self.delegate(
                    BACKEND_CONTEXT + words, partial(self.progress, delegation_id)
                )
                record["agent_completed_ns"] = time.monotonic_ns()
                record.update(details, reply=reply)
                result = outcome(reply)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("Handoff %s failed in call %s", delegation_id, self.id)
                record["error"] = type(error).__name__
                result = FAILED
            finally:
                self.working = None
                self.ok = None
                self.refresh()
        self.say(result, delegation_id, record)

    async def progress(self, delegation_id: str, event: dict) -> None:
        if event["type"] == "tool.started":
            tool = event.get("tool") or "a tool"
            self.kept["progress"] = (f"{PROGRESS} using {tool}.", delegation_id)
        elif event["type"] == "approval.request":
            self.ok = (event["run_id"], event["request_id"])
            self.say(ok_question(event.get("command") or "a tool"), delegation_id)

    def refresh(self) -> None:
        found = []
        if self.working:
            found.append(f"Working on: {self.working}")
        found += [f"Queued: {words}" for words in self.queued]
        if count := self.background():
            found.append(f"{count} background task{'s' if count > 1 else ''} running.")
        summary = "\n".join(found) or "Nothing is running."
        if summary != self.summary:
            self.summary = summary
            self.kept["summary"] = (f"{GOING_ON}\n{summary}", None)

    def confide(self) -> None:
        kept, self.kept = self.kept, {}
        for text, delegation_id in kept.values():
            self.think(text, delegation_id)

    def think(self, text: str, delegation_id: str | None) -> None:
        if self.connection is None:
            return
        self.notes.append({"text": text, "given_ns": time.monotonic_ns()})
        self.spawn(
            self.connection.session.thinking.append(
                content=text, delegation_id=self.current(delegation_id, self.generation)
            )
        )

    def current(self, delegation_id: str | None, generation: int) -> str | None:
        return delegation_id if generation == self.generation else None

    def say(
        self, text: str, delegation_id: str | None = None, record: dict | None = None
    ) -> None:
        self.owed.append(Update(text, delegation_id, self.generation, record=record))
        self.pace()

    def interrupted(self) -> None:
        last = self.given
        if last and self.voice_ns < last.given_ns:
            self.given = None
            self.owe_again(last)
        self.pace()

    def owe_again(self, last: Update) -> None:
        if not last.again:
            self.owed.insert(
                0,
                Update(
                    f"{UNSAID} {last.text}",
                    last.delegation_id,
                    last.generation,
                    again=True,
                    record=last.record,
                ),
            )

    def pace(self) -> None:
        if self.pacer:
            self.pacer.cancel()
            self.pacer = None
        if self.connection is None:
            return
        now = time.monotonic_ns()
        last = self.given
        if last and self.voice_ns < last.given_ns:
            unsaid = max(last.given_ns + SAYING_NS, self.talked_ns + PERSON_TALKING_NS)
            if unsaid <= now:
                self.given = None
                self.owe_again(last)
            elif not self.owed:
                self.later(unsaid - now)
                return
        if not self.owed:
            return
        update = self.owed[0]
        free = max(
            self.talked_ns + PERSON_TALKING_NS,
            self.voice_ns + VOICE_TALKING_NS,
            self.given.given_ns + SAYING_NS if self.given else 0,
        )
        overdue = update.owed_ns + OWED_MAX_NS
        if free > now and now < overdue:
            self.later(min(free, overdue) - now)
            return
        self.owed.pop(0)
        update.given_ns = now
        self.given = update
        if update.record is not None:
            update.record.setdefault("commentary_sent_ns", now)
        self.updates.append({"text": update.text, "given_ns": now})
        self.spawn(
            self.connection.session.commentary.append(
                content=update.text,
                delegation_id=self.current(update.delegation_id, update.generation),
            )
        )
        self.pace()

    def later(self, delay_ns: int) -> None:
        self.pacer = asyncio.get_running_loop().call_later(
            max(0.02, delay_ns / 1e9), self.pace
        )

    def report(self) -> dict:
        return {
            "call_id": self.id,
            "transport": self.transport,
            "session_id": self.session_id,
            "rate": self.rate,
            "seconds": self.seconds,
            "server": self.timing.snapshot(),
            "delegations": self.delegations,
            "updates": self.updates,
            "notes": self.notes,
            "words": [asdict(part) for part in self.words],
        }
