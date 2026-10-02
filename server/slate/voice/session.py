import asyncio
import json
import logging
from collections.abc import Coroutine
from contextlib import AsyncExitStack, aclosing
from typing import Any
from uuid import uuid4

from livekit import rtc

from slate.agent import create_agent
from slate.voice.audio import SAMPLE_RATE, read_wav
from slate.voice.client import speak, transcribe_stream
from slate.voice.settings import (
    REPLY_TOPIC,
    TRANSCRIPT_TOPIC,
    WORKER_IDENTITY,
    VoiceSettings,
)
from slate.voice.turn import Turn

logger = logging.getLogger("slate.voice.session")


class VoiceSession:
    def __init__(self, settings: VoiceSettings, *, profile: bool = False) -> None:
        self.settings = settings
        self.profile = profile
        self.id = uuid4().hex
        self.room_name = f"slate-{self.id}"
        self.device_identity = f"device-{self.id}"
        self.room = rtc.Room()
        self.agent = create_agent()
        self.speaker = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=100)
        self.closed = False
        self.close_done = asyncio.Event()
        self.audio_ready = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.turn: Turn | None = None
        self.turn_task: asyncio.Task | None = None
        self.reader: asyncio.Task | None = None

    def spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.task_finished)
        return task

    def task_finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()):
            logger.error("Session task failed", exc_info=error)

    async def connect(self) -> dict[str, str]:
        self.room.on("track_subscribed", self.track_subscribed)
        self.room.on("track_unsubscribed", self.track_unsubscribed)
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
                "start_turn", self.start_turn
            )
            self.room.local_participant.register_rpc_method("end_turn", self.end_turn)
            self.room.local_participant.register_rpc_method(
                "cancel_turn", self.cancel_turn
            )
            self.room.local_participant.register_rpc_method(
                "approve_tool", self.approve_tool
            )
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
            and self.reader is None
        ):
            self.reader = self.spawn(self.read_audio(track))
            self.audio_ready.set()

    def track_unsubscribed(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if participant.identity == self.device_identity:
            self.audio_ready.clear()
            if self.reader:
                self.reader.cancel()
                self.reader = None
            self.spawn(self.abort("Microphone disconnected"))

    def participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        if participant.identity == self.device_identity:
            self.spawn(self.close())

    def disconnected(self, reason: rtc.DisconnectReason.ValueType) -> None:
        if not self.closed:
            self.spawn(self.close())

    async def read_audio(self, track: rtc.RemoteTrack) -> None:
        stream = rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=80
        )
        try:
            async for event in stream:
                if self.turn is not None:
                    try:
                        self.turn.push(event.frame.data.tobytes())
                    except ValueError as error:
                        await self.abort(str(error))
        except Exception:
            logger.exception("Microphone stream failed")
            await self.abort("Microphone stream failed")
        finally:
            await stream.aclose()

    def authorize(self, data: rtc.RpcInvocationData) -> None:
        if self.closed or data.caller_identity != self.device_identity:
            raise rtc.RpcError(1501, "This session belongs to another device")

    def requested_turn(self, data: rtc.RpcInvocationData) -> Turn:
        self.authorize(data)
        if self.turn is None or data.payload != self.turn.id:
            raise rtc.RpcError(1502, "This recording has ended")
        return self.turn

    async def start_turn(self, data: rtc.RpcInvocationData) -> str:
        self.authorize(data)
        try:
            await asyncio.wait_for(self.audio_ready.wait(), timeout=5)
        except TimeoutError as error:
            raise rtc.RpcError(1503, "Publish a microphone track first") from error
        self.authorize(data)
        if self.turn is not None:
            raise rtc.RpcError(1504, "Finish or cancel the current recording first")
        turn = Turn()
        self.turn = turn
        self.turn_task = self.spawn(self.transcribe(turn))
        return turn.id

    async def end_turn(self, data: rtc.RpcInvocationData) -> str:
        turn = self.requested_turn(data)
        if not turn.ending:
            turn.ending = True
            turn.timing.mark("end_requested")
            await asyncio.sleep(0.3)
            turn.finish()
        return turn.id

    async def cancel_turn(self, data: rtc.RpcInvocationData) -> str:
        self.authorize(data)
        if self.turn is None:
            return "ok"
        self.requested_turn(data)
        await self.abort()
        return "ok"

    async def approve_tool(self, data: rtc.RpcInvocationData) -> str:
        self.authorize(data)
        try:
            payload = json.loads(data.payload)
            if self.turn is None or payload["turn_id"] != self.turn.id:
                raise ValueError("This recording has ended")
            await self.agent.approve(
                payload["run_id"], payload["request_id"], payload["choice"]
            )
        except (ValueError, KeyError, TypeError) as error:
            raise rtc.RpcError(1502, str(error)) from error
        return "ok"

    async def agent_progress(self, turn: Turn, event: dict) -> None:
        if self.turn is not turn:
            return
        if event["type"] == "approval.request":
            await self.publish(
                turn,
                topic="slate.agent",
                approval={
                    "run_id": event["run_id"],
                    "request_id": event["request_id"],
                    "command": event.get("command", "Tool permission requested"),
                },
            )
        elif event["type"] == "tool.started":
            await self.publish(
                turn, topic="slate.agent", tool=event.get("tool", "tool")
            )

    async def publish(
        self, turn: Turn, topic: str = TRANSCRIPT_TOPIC, **fields: Any
    ) -> None:
        await self.room.local_participant.publish_data(
            json.dumps({"turn_id": turn.id, **fields}),
            reliable=True,
            destination_identities=[self.device_identity],
            topic=topic,
        )

    async def play(self, wav: bytes, turn: Turn) -> None:
        pcm = read_wav(wav)
        frame_bytes = SAMPLE_RATE * 2 // 50
        for offset in range(0, len(pcm), frame_bytes):
            chunk = pcm[offset : offset + frame_bytes]
            turn.timing.mark("reply_first_enqueue")
            await self.speaker.capture_frame(
                rtc.AudioFrame(chunk, SAMPLE_RATE, 1, len(chunk) // 2)
            )
        await self.speaker.wait_for_playout()
        turn.timing.mark("reply_playout_done")

    async def transcribe(self, turn: Turn) -> None:
        text = ""
        stage = "transcription"
        stt_timing = {} if self.profile else None
        tts_timing = {} if self.profile else None
        try:
            logger.info("Transcription started for turn %s", turn.id)
            turn.timing.mark("stt_requested")
            async with asyncio.timeout(180):
                async with aclosing(
                    transcribe_stream(turn.chunks(), timings=stt_timing)
                ) as stream:
                    async for piece in stream:
                        turn.timing.mark("stt_first_text")
                        text += piece
                        await self.publish(turn, text=text.strip(), final=False)
            turn.timing.mark("stt_completed")
            logger.info("Transcription finished for turn %s", turn.id)
            await self.publish(turn, text=text.strip(), final=True)
            turn.timing.mark("transcript_published")
            if text.strip():
                stage = "agent reply"
                turn.timing.mark("agent_requested")
                async with asyncio.timeout(310):
                    reply = await self.agent.run(
                        text.strip(), lambda event: self.agent_progress(turn, event)
                    )
                turn.timing.mark("agent_completed")
                logger.info("Agent replied for turn %s", turn.id)
                await self.publish(turn, topic=REPLY_TOPIC, text=reply, final=False)
                turn.timing.mark("reply_text_published")
                stage = "speech generation"
                async with asyncio.timeout(180):
                    turn.timing.mark("tts_requested")
                    audio = await speak(reply, timings=tts_timing)
                    turn.timing.mark("tts_completed")
                    logger.info("Speech generated for turn %s", turn.id)
                    await self.play(audio, turn)
                logger.info("Speech played for turn %s", turn.id)
                fields = {}
                if self.profile:
                    fields["timing"] = {
                        "server": turn.timing.snapshot(),
                        "agent": self.agent.timings,
                        "agent_runtime": self.agent.last_run.get("runtime"),
                        "stt": stt_timing,
                        "tts": tts_timing,
                    }
                await self.publish(
                    turn, topic=REPLY_TOPIC, text=reply, final=True, **fields
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s failed for turn %s", stage, turn.id)
            await self.publish(
                turn,
                topic=TRANSCRIPT_TOPIC if stage == "transcription" else REPLY_TOPIC,
                error=f"{stage.capitalize()} failed. Try another recording.",
            )
        finally:
            turn.finish()
            if self.turn is turn:
                self.turn = None

    async def abort(self, message: str | None = None) -> None:
        self.speaker.clear_queue()
        turn = self.turn
        if turn is None:
            return
        turn.finish()
        if self.turn_task:
            self.turn_task.cancel()
            await asyncio.gather(self.turn_task, return_exceptions=True)
        if self.turn is turn:
            self.turn = None
        if message and not self.closed:
            await self.publish(turn, error=message)

    async def expire(self) -> None:
        await asyncio.sleep(600)
        await self.close()

    async def close(self) -> None:
        if self.closed:
            await self.close_done.wait()
            return
        self.closed = True
        if self.turn:
            self.turn.finish()
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


class VoiceSessions:
    def __init__(self) -> None:
        self.current: VoiceSession | None = None
        self.lock = asyncio.Lock()

    async def create(self, *, profile: bool = False) -> dict[str, str]:
        async with self.lock:
            if self.current is not None and not self.current.closed:
                raise ValueError("A microphone session is already connected")
            session = VoiceSession(VoiceSettings.from_env(), profile=profile)
            result = await session.connect()
            self.current = session
            return result

    async def close(self, session_id: str | None = None) -> None:
        if self.current and (session_id is None or self.current.id == session_id):
            await self.current.close()
