import asyncio
import json
import logging
from collections.abc import Coroutine
from contextlib import AsyncExitStack, aclosing
from typing import Any
from uuid import uuid4

from livekit import rtc

from slate.agent import create_agent
from slate.device import DeviceCommand, DeviceSDK
from slate.voice.audio import SAMPLE_RATE
from slate.voice.client import speak_stream, transcribe_stream
from slate.voice.reply import Sentences
from slate.voice.settings import (
    REPLY_TOPIC,
    TRANSCRIPT_TOPIC,
    WORKER_IDENTITY,
    VoiceSettings,
)
from slate.voice.turn import Turn

logger = logging.getLogger("slate.voice.session")
CAPTURE_DRAIN_SECONDS = 1.0


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
        self.turn_lock = asyncio.Lock()

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
        async with self.turn_lock:
            self.authorize(data)
            await self._abort()
            self.authorize(data)
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
        async with self.turn_lock:
            self.authorize(data)
            if self.turn is None:
                return "ok"
            self.requested_turn(data)
            await self._abort()
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
        if self.closed or self.turn is not turn:
            return
        await self.room.local_participant.publish_data(
            json.dumps({"turn_id": turn.id, **fields}),
            reliable=True,
            destination_identities=[self.device_identity],
            topic=topic,
        )

    def device_sdk(self, turn: Turn) -> DeviceSDK:
        async def execute(command: DeviceCommand) -> dict:
            result = await self.room.local_participant.perform_rpc(
                destination_identity=self.device_identity,
                method="device.command",
                payload=command.model_dump_json(),
                response_timeout=5,
            )
            return json.loads(result)

        return DeviceSDK(
            turn.scope,
            turn.id,
            execute,
            lambda: not self.closed and self.turn is turn,
        )

    async def play(self, pcm: bytes, turn: Turn) -> None:
        frame_bytes = SAMPLE_RATE * 2 // 50
        for offset in range(0, len(pcm), frame_bytes):
            if self.closed or self.turn is not turn:
                return
            chunk = pcm[offset : offset + frame_bytes]
            turn.timing.mark("reply_first_enqueue")
            capture = asyncio.create_task(
                self.speaker.capture_frame(
                    rtc.AudioFrame(chunk, SAMPLE_RATE, 1, len(chunk) // 2)
                )
            )
            try:
                await asyncio.shield(capture)
            except asyncio.CancelledError:
                deadline = asyncio.get_running_loop().time() + CAPTURE_DRAIN_SECONDS
                while not capture.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(capture),
                            max(0, deadline - asyncio.get_running_loop().time()),
                        )
                    except asyncio.CancelledError:
                        continue
                    except TimeoutError:
                        logger.error("Native audio capture stalled; retiring session")
                        closing = self.closed
                        if not closing:
                            self._stop()
                        try:
                            await self._close_audio()
                        finally:
                            capture.cancel()
                            if not closing:
                                self.spawn(self._finish_close())
                            await asyncio.gather(capture, return_exceptions=True)
                        raise asyncio.CancelledError from None
                capture.result()
                raise

    async def respond(self, turn: Turn, text: str, tts_timing: list) -> str:
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=4)
        sentences = Sentences()
        streamed = ""
        complete = False

        async def enqueue(pieces: list[str]) -> None:
            for piece in pieces:
                await queue.put(piece)

        async def progress(event: dict) -> None:
            nonlocal streamed, complete
            if self.turn is not turn:
                return
            await self.agent_progress(turn, event)
            if event["type"] == "reply.delta":
                piece = event["delta"]
                streamed += piece
                await self.publish(turn, topic=REPLY_TOPIC, text=streamed, final=False)
                turn.timing.mark("reply_text_published")
                await enqueue(sentences.feed(piece))
            elif event["type"] == "answer.complete":
                answer = event["text"]
                if streamed and streamed.strip() != answer.strip():
                    raise RuntimeError("Final agent answer differs from spoken deltas")
                if not streamed:
                    streamed = answer
                    await self.publish(
                        turn, topic=REPLY_TOPIC, text=answer, final=False
                    )
                    turn.timing.mark("reply_text_published")
                    await enqueue(sentences.feed(answer))
                await enqueue(sentences.feed("", final=True))
                complete = True

        async def generate() -> str:
            context = (
                f"This turn controls a Slate device. Its opaque scope is {turn.scope}. "
                "Use the slate-device MCP SDK to control it when requested. "
                "Only report device changes after a successful acknowledgment. "
                "Device text is printable ASCII, at most 64 characters. "
                "Orb color is #RRGGBB and radius is 10 through 45 pixels. "
                "The scope expires when this turn ends."
            )
            turn.timing.mark("agent_requested")
            async with asyncio.timeout(310):
                answer = await self.agent.run(text, progress, device_context=context)
            turn.timing.mark("agent_completed")
            if not complete:
                await progress({"type": "answer.complete", "text": answer})
            await queue.put(None)
            return answer

        async def synthesize() -> None:
            while (piece := await queue.get()) is not None:
                timing = {} if self.profile else None
                if timing is not None:
                    tts_timing.append(timing)
                turn.timing.mark("tts_requested")
                async with asyncio.timeout(180):
                    async with aclosing(speak_stream(piece, timings=timing)) as stream:
                        async for pcm in stream:
                            turn.timing.mark("tts_first_audio")
                            await self.play(pcm, turn)
            turn.timing.mark("tts_completed")
            await self.speaker.wait_for_playout()
            turn.timing.mark("reply_playout_done")

        async with asyncio.TaskGroup() as tasks:
            answer = tasks.create_task(generate())
            tasks.create_task(synthesize())
        return answer.result()

    async def transcribe(self, turn: Turn) -> None:
        text = ""
        stage = "transcription"
        stt_timing = {} if self.profile else None
        tts_timing = []
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
                stage = "assistant response"
                reply = await self.respond(turn, text.strip(), tts_timing)
                logger.info("Speech played for turn %s", turn.id)
                fields = {}
                if self.profile:
                    fields["timing"] = {
                        "server": turn.timing.snapshot(),
                        "agent": self.agent.timings,
                        "agent_runtime": self.agent.last_run.get("runtime"),
                        "stt": stt_timing,
                        "tts": {"segments": tts_timing},
                    }
                await self.publish(
                    turn, topic=REPLY_TOPIC, text=reply, final=True, **fields
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s failed for turn %s", stage, turn.id)
            if self.turn is turn:
                self.speaker.clear_queue()
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
        async with self.turn_lock:
            await self._abort(message)

    async def _abort(self, message: str | None = None) -> None:
        turn = self.turn
        task = self.turn_task
        self.turn = None
        self.turn_task = None
        if not self.closed:
            self.speaker.clear_queue()
        if turn is None:
            return
        turn.finish()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not self.closed:
            self.speaker.clear_queue()
            await self.room.local_participant.publish_data(
                json.dumps(
                    {
                        "turn_id": turn.id,
                        "cancelled": True,
                        **({"error": message} if message else {}),
                    }
                ),
                reliable=True,
                destination_identities=[self.device_identity],
                topic=REPLY_TOPIC,
            )

    async def expire(self) -> None:
        await asyncio.sleep(600)
        await self.close()

    def _stop(self) -> None:
        self.closed = True
        self._audio_retired = False
        self.speaker.clear_queue()
        if self.turn:
            self.turn.finish()
            self.turn = None

    async def _close_audio(self) -> None:
        if not self._audio_retired:
            self._audio_retired = True
            await self.speaker.aclose()

    async def _finish_close(self) -> None:
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        try:
            async with AsyncExitStack() as cleanup:
                cleanup.push_async_callback(self.room.disconnect)
                cleanup.push_async_callback(self._close_audio)
                cleanup.push_async_callback(self.agent.close)
                await asyncio.gather(*tasks, return_exceptions=True)
                if not self._audio_retired:
                    self.speaker.clear_queue()
        finally:
            self.close_done.set()

    async def close(self) -> None:
        if self.closed:
            await self.close_done.wait()
            return
        self._stop()
        await self._finish_close()


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
