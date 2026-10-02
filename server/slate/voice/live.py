import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from livekit import rtc
from openai import AsyncOpenAI

from slate.agent import create_agent
from slate.voice.audio import AUDIBLE_PEAK, SAMPLE_BYTES, SAMPLE_RATE, peak
from slate.voice.settings import (
    REPLY_TOPIC,
    TRANSCRIPT_TOPIC,
    WORKER_IDENTITY,
    VoiceSettings,
)
from slate.voice.timing import Timeline

logger = logging.getLogger("slate.voice.live")
INSTRUCTIONS = Path(__file__).with_name("live.md").read_text()
SESSION = {
    "model": "gpt-live-1",
    "instructions": INSTRUCTIONS,
    "audio": {
        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
        "output": {"voice": "marin"},
    },
    "delegation": {"type": "client"},
}
BACKEND_CONTEXT = (
    "Slate's voice model will say your answer out loud. The transcript below "
    "comes from speech recognition, so it may have mistakes. Answer the user's "
    "most recent request.\n\n"
)
FAILED_REPLY = "I couldn't finish that. Please try again."
FRAME_BYTES = SAMPLE_RATE * SAMPLE_BYTES // 50
ONSET_GAP_NS = 300_000_000
TRANSCRIPT_QUIET_NS = 250_000_000
TRANSCRIPT_LIMIT_NS = 1_000_000_000


@dataclass
class Words:
    role: str
    start_ms: int
    end_ms: int
    text: str


def transcript(words: list[Words], since_ms: int, until_ms: float) -> str:
    lines: list[tuple[str, str]] = []
    recent = (
        part for part in words if since_ms < part.end_ms and part.start_ms < until_ms
    )
    for part in sorted(recent, key=lambda part: part.start_ms):
        if lines and lines[-1][0] == part.role:
            lines[-1] = (part.role, lines[-1][1] + part.text)
        else:
            lines.append((part.role, part.text))
    return "\n".join(f"{role}: {text.strip()}" for role, text in lines if text.strip())


class LiveConversation:
    """One GPT-Live session: microphone PCM goes in, Slate's voice comes out, and
    each delegated request runs on the Slate agent."""

    def __init__(
        self,
        agent,
        *,
        speak: Callable[[bytes], None],
        publish: Callable[..., Awaitable[None]],
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.agent = agent
        self.speak = speak
        self.publish = publish
        self.client = client
        self.connection = None
        self.started = asyncio.Event()
        self.timing = Timeline()
        self.session_id: str | None = None
        self.usage: dict | None = None
        self.words: list[Words] = []
        self.heard_ns = 0
        self.delegated_ms = -1
        self.delegations: list[dict] = []
        self.delegation_order = asyncio.Lock()
        self.onsets_ns: list[int] = []
        self.audible_ns = 0
        self.tasks: set[asyncio.Task] = set()

    async def run(self, audio: AsyncIterator[bytes]) -> None:
        self.timing.mark("live_connect_requested")
        client = self.client or AsyncOpenAI()
        try:
            async with client.live.connect(max_retries=0) as connection:
                self.connection = connection
                await connection.session.start(session=SESSION, event_id="slate_start")
                async with asyncio.TaskGroup() as group:
                    group.create_task(self.receive())
                    group.create_task(self.send(audio))
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.client is None:
                await client.close()

    async def send(self, audio: AsyncIterator[bytes]) -> None:
        async with asyncio.timeout(15):
            await self.started.wait()
        async for chunk in audio:
            self.timing.mark("input_first_sent")
            await self.connection.session.input_audio.append(
                audio=base64.b64encode(chunk).decode()
            )
        self.timing.mark("input_ended")
        await self.connection.session.close()

    async def receive(self) -> None:
        async for event in self.connection:
            kind = event.type
            if kind == "session.started":
                self.timing.mark("live_started")
                self.session_id = event.session.id
                logger.info("GPT-Live session %s started", self.session_id)
                self.started.set()
            elif kind == "session.output_audio.delta":
                self.play(base64.b64decode(event.delta))
            elif kind == "session.input_transcript.delta":
                self.heard_ns = time.monotonic_ns()
                self.words.append(
                    Words("User", event.start_ms, event.end_ms, event.delta)
                )
                await self.publish(TRANSCRIPT_TOPIC, text=event.delta)
            elif kind == "session.output_transcript.delta":
                self.words.append(
                    Words("Slate", event.start_ms, event.end_ms, event.delta)
                )
                await self.publish(REPLY_TOPIC, text=event.delta)
            elif kind == "session.delegation.created":
                if event.delegation.target == "client":
                    task = asyncio.create_task(
                        self.delegate(event.delegation.id, event.offset_ms)
                    )
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            elif kind == "session.commentary.appended":
                for record in self.delegations:
                    if record.get("event_id") == event.client_event_id:
                        record["appended_ns"] = time.monotonic_ns()
                        record["appended_ms"] = event.start_ms
            elif kind == "error":
                logger.error(
                    "GPT-Live error for %s: %s %s",
                    event.client_event_id,
                    event.error.code,
                    event.error.message,
                )
                if event.client_event_id is None:
                    raise RuntimeError(f"slate.live: {event.error.code}")
            elif kind == "session.closed":
                self.usage = event.usage.model_dump() if event.usage else None
                self.timing.mark("live_closed")
                return
            elif kind == "info":
                logger.info("GPT-Live info %s: %s", event.code, event.message)
        raise RuntimeError("slate.live: GPT-Live disconnected before session.closed")

    def play(self, pcm: bytes) -> None:
        now = time.monotonic_ns()
        self.timing.mark("output_first_audio")
        if peak(pcm) > AUDIBLE_PEAK:
            if now - self.audible_ns > ONSET_GAP_NS:
                self.onsets_ns.append(now)
            self.audible_ns = now
        self.speak(pcm)

    async def settle_transcript(self) -> None:
        deadline = time.monotonic_ns() + TRANSCRIPT_LIMIT_NS
        while (now := time.monotonic_ns()) < deadline:
            quiet = now - self.heard_ns
            if quiet >= TRANSCRIPT_QUIET_NS:
                return
            await asyncio.sleep((TRANSCRIPT_QUIET_NS - quiet) / 1e9)

    async def delegate(self, delegation_id: str, offset_ms: int) -> None:
        async with self.delegation_order:
            await self.answer(delegation_id, offset_ms)

    async def answer(self, delegation_id: str, offset_ms: int) -> None:
        record: dict[str, Any] = {
            "id": delegation_id,
            "event_id": f"result-{len(self.delegations)}",
            "offset_ms": offset_ms,
            "created_ns": time.monotonic_ns(),
        }
        self.delegations.append(record)
        since_ms, self.delegated_ms = self.delegated_ms, offset_ms
        try:
            await self.settle_transcript()
            record["transcript_ready_ns"] = time.monotonic_ns()
            request = transcript(self.words, since_ms, offset_ms)
            record["request"] = request
            if not request:
                raise ValueError("No transcript arrived before this delegation")
            record["agent_requested_ns"] = time.monotonic_ns()
            reply = await self.agent.run(BACKEND_CONTEXT + request)
            record["agent_completed_ns"] = time.monotonic_ns()
            record["agent"] = self.agent.timings
            record["reply"] = reply
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("Delegation %s failed", delegation_id)
            record["error"] = type(error).__name__
            reply = FAILED_REPLY
        record["commentary_sent_ns"] = time.monotonic_ns()
        await self.connection.session.commentary.append(
            event_id=record["event_id"], delegation_id=delegation_id, content=reply
        )

    def report(self) -> dict:
        return {
            "session_id": self.session_id,
            "server": self.timing.snapshot(),
            "delegations": self.delegations,
            "onsets_ns": self.onsets_ns,
            "words": [asdict(part) for part in self.words],
            "usage": self.usage,
            "agent_runtime": self.agent.last_run.get("runtime"),
        }


class LiveSession:
    """A LiveKit room where the device's microphone streams continuously to
    GPT-Live and Slate's voice streams back."""

    def __init__(self, settings: VoiceSettings) -> None:
        self.settings = settings
        self.id = uuid4().hex
        self.room_name = f"slate-{self.id}"
        self.device_identity = f"device-{self.id}"
        self.room = rtc.Room()
        self.speaker = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=100)
        self.voice: asyncio.Queue[bytes] = asyncio.Queue()
        self.agent = create_agent()
        self.conversation = LiveConversation(
            self.agent, speak=self.voice.put_nowait, publish=self.publish
        )
        self.closed = False
        self.close_done = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.call: asyncio.Task | None = None

    def spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def connect(self) -> dict[str, str]:
        self.room.on("track_subscribed", self.track_subscribed)
        self.room.on("participant_disconnected", self.participant_disconnected)
        self.room.on("disconnected", self.disconnected)
        token = self.settings.token(self.room_name, WORKER_IDENTITY, worker=True)
        try:
            await self.room.connect(
                self.settings.url, token, rtc.RoomOptions(connect_timeout=10)
            )
            track = rtc.LocalAudioTrack.create_audio_track("slate-reply", self.speaker)
            await self.room.local_participant.publish_track(track)
            self.room.local_participant.register_rpc_method(
                "live_report", self.live_report
            )
            self.spawn(self.play())
            self.spawn(self.expire())
        except BaseException:
            await self.close()
            raise
        return {
            "session_id": self.id,
            "server_url": self.settings.url,
            "participant_token": self.settings.token(
                self.room_name, self.device_identity
            ),
            "worker_identity": WORKER_IDENTITY,
        }

    def track_subscribed(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if (
            participant.identity == self.device_identity
            and track.kind == rtc.TrackKind.KIND_AUDIO
            and publication.source == rtc.TrackSource.SOURCE_MICROPHONE
            and self.call is None
        ):
            self.call = self.spawn(self.converse(track))

    def participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        if participant.identity == self.device_identity:
            self.spawn(self.close())

    def disconnected(self, reason: rtc.DisconnectReason.ValueType) -> None:
        if not self.closed:
            self.spawn(self.close())

    async def converse(self, track: rtc.RemoteTrack) -> None:
        stream = rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=20
        )

        async def microphone() -> AsyncIterator[bytes]:
            async for event in stream:
                yield event.frame.data.tobytes()

        try:
            await self.conversation.run(microphone())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("GPT-Live conversation failed for session %s", self.id)
            if not self.closed:
                await self.publish(REPLY_TOPIC, error="Live conversation failed.")
        finally:
            await stream.aclose()

    async def play(self) -> None:
        pending = b""
        while True:
            pending += await self.voice.get()
            while len(pending) >= FRAME_BYTES:
                frame, pending = pending[:FRAME_BYTES], pending[FRAME_BYTES:]
                await self.speaker.capture_frame(
                    rtc.AudioFrame(frame, SAMPLE_RATE, 1, FRAME_BYTES // SAMPLE_BYTES)
                )

    async def live_report(self, data: rtc.RpcInvocationData) -> str:
        if data.caller_identity != self.device_identity:
            raise rtc.RpcError(1501, "This session belongs to another device")
        return json.dumps(self.conversation.report())

    async def publish(self, topic: str, **fields: Any) -> None:
        await self.room.local_participant.publish_data(
            json.dumps({"session_id": self.id, **fields}),
            reliable=True,
            destination_identities=[self.device_identity],
            topic=topic,
        )

    async def expire(self) -> None:
        await asyncio.sleep(600)
        await self.close()

    async def close(self) -> None:
        if self.closed:
            await self.close_done.wait()
            return
        self.closed = True
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        try:
            async with AsyncExitStack() as cleanup:
                cleanup.push_async_callback(self.room.disconnect)
                cleanup.push_async_callback(self.speaker.aclose)
                cleanup.push_async_callback(self.agent.close)
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.close_done.set()
