import asyncio
import json
import logging
import os
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack, aclosing
from functools import partial
from typing import Any, Literal
from uuid import uuid4

import numpy as np
import soxr
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from slate.agent import create_agent
from slate.agent.agent import BACKGROUND_PROMPT
from slate.device import DeviceCommand, DeviceSDK, agent_context
from slate.voice.audio import SAMPLE_BYTES, SAMPLE_RATE
from slate.voice.client import speak_stream, transcribe_stream
from slate.voice.live import LiveCall
from slate.voice.reply import Sentences
from slate.voice.turn import Turn

logger = logging.getLogger("slate.voice.session")
SPOKEN_END = "\n---"
REPLY_FRAME_BYTES = SAMPLE_RATE * SAMPLE_BYTES // 50
REPLY_LEAD_SECONDS = 0.3
RECEIPT_SECONDS = 5
HANGUP_SECONDS = 5
KEPT_REPORTS = 50
PROFILE = os.getenv("SLATE_PROFILE") == "1"


class Hello(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["hello"]
    rate: int = Field(default=16_000, ge=8_000, le=48_000)
    profile: StrictBool = False


def spoken_part(text: str, final: bool) -> tuple[str, bool]:
    before, marker, _ = text.partition(SPOKEN_END)
    if marker:
        return before, True
    if not final:
        for size in range(len(SPOKEN_END), 0, -1):
            if text.endswith(SPOKEN_END[:size]):
                return text[:-size], False
    return text, False


class VoiceSession:
    """One connected device. Its WebSocket carries microphone audio and control
    messages in, and transcripts, replies, speech and device commands out."""

    def __init__(self, socket: WebSocket, hello: Hello) -> None:
        self.socket = socket
        self.profile = hello.profile or PROFILE
        self.send_timing = hello.profile
        self.reports: dict[str, dict] = {}
        self.id = uuid4().hex
        self.agent = create_agent()
        self.resampler = (
            None
            if hello.rate == SAMPLE_RATE
            else soxr.ResampleStream(hello.rate, SAMPLE_RATE, 1, dtype="int16")
        )
        self.closed = False
        self.close_done = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.receipts: dict[str, asyncio.Future] = {}
        self.turn: Turn | None = None
        self.turn_task: asyncio.Task | None = None
        self.turn_lock = asyncio.Lock()
        self.watcher: asyncio.Task | None = None
        self.call: LiveCall | None = None
        self.call_task: asyncio.Task | None = None
        self.playhead = 0.0

    def spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.task_finished)
        return task

    def task_finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()):
            logger.error("Session task failed", exc_info=error)

    async def serve(self) -> None:
        try:
            while True:
                message = await self.socket.receive()
                if message["type"] == "websocket.disconnect":
                    logger.info("Device %s disconnected", self.id)
                    return
                if message.get("bytes") is not None:
                    await self.audio(message["bytes"])
                elif message.get("text") is not None:
                    await self.control(json.loads(message["text"]))
        except WebSocketDisconnect:
            logger.info("Device %s disconnected", self.id)
        finally:
            await self.close()

    async def audio(self, pcm: bytes) -> None:
        call = self.call
        turn = self.turn
        if call is None and (turn is None or not turn.receiving or turn.announcement):
            return
        if self.resampler is not None:
            if len(pcm) % SAMPLE_BYTES:
                if call is not None:
                    logger.warning("Dropped partial samples in live call %s", call.id)
                    return
                await self.abort("Audio must contain complete 16-bit samples")
                return
            pcm = self.resampler.resample_chunk(
                np.frombuffer(pcm, dtype="<i2")
            ).tobytes()
            if not pcm:
                return
        if call is not None:
            call.push(pcm)
            return
        try:
            turn.push(pcm)
        except ValueError as error:
            logger.warning("Rejected microphone audio for turn %s: %s", turn.id, error)
            await self.abort(str(error))

    async def control(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "start":
            await self.start_turn()
        elif kind == "end":
            await self.end_turn()
        elif kind == "cancel":
            await self.abort()
        elif kind == "live":
            await self.start_call()
        elif kind == "hangup":
            await self.end_call()
        elif kind == "receipt":
            waiter = self.receipts.pop(str(message.get("request_id")), None)
            if waiter is None or waiter.done():
                logger.warning("Ignored unexpected device receipt %s", message)
            else:
                waiter.set_result(message)
        elif kind == "approve":
            await self.approve(message)
        else:
            logger.warning("Ignored unknown device message type %r", kind)

    async def send(self, event: dict) -> None:
        if self.closed:
            return
        async with self.send_lock:
            await self.socket.send_text(json.dumps(event))

    async def send_audio(self, pcm: bytes) -> None:
        if self.closed:
            return
        async with self.send_lock:
            await self.socket.send_bytes(pcm)

    async def start_turn(self) -> None:
        async with self.turn_lock:
            if self.call is not None:
                logger.warning("Device %s asked to talk during a live call", self.id)
                return
            await self._abort()
            turn = Turn()
            self.turn = turn
            if self.resampler is not None:
                self.resampler.clear()
            self.turn_task = self.spawn(self.transcribe(turn))
        await self.send({"type": "turn", "turn_id": turn.id})

    async def end_turn(self) -> None:
        turn = self.turn
        if turn is None or turn.ending or turn.announcement:
            return
        turn.ending = True
        turn.timing.mark("end_requested")
        if self.resampler is not None:
            tail = self.resampler.resample_chunk(np.zeros(0, dtype="<i2"), last=True)
            if tail.size:
                turn.push(tail.tobytes())
        turn.finish()

    async def approve(self, message: dict) -> None:
        try:
            if self.turn is None or message["turn_id"] != self.turn.id:
                raise ValueError("This recording has ended")
            await self.agent.approve(
                message["run_id"], message["request_id"], message["choice"]
            )
        except (ValueError, KeyError, TypeError) as error:
            logger.warning("Approval rejected: %s", error)
            await self.send(
                {
                    "type": "error",
                    "turn_id": message.get("turn_id"),
                    "message": str(error),
                }
            )

    async def agent_progress(self, turn: Turn, event: dict) -> None:
        if self.turn is not turn:
            return
        if event["type"] == "approval.request":
            await self.publish(
                turn,
                "approval",
                run_id=event["run_id"],
                request_id=event["request_id"],
                command=event.get("command", "Tool permission requested"),
            )
        elif event["type"] == "tool.started":
            await self.publish(turn, "tool", tool=event.get("tool", "tool"))

    async def publish(self, turn: Turn, kind: str, **fields: Any) -> None:
        if self.closed or self.turn is not turn:
            return
        if turn.announcement:
            fields["announcement"] = True
        await self.send({"type": kind, "turn_id": turn.id, **fields})

    def device_sdk(self, turn: Turn) -> DeviceSDK:
        async def execute(command: DeviceCommand) -> dict:
            waiter = asyncio.get_running_loop().create_future()
            self.receipts[command.request_id] = waiter
            try:
                await self.send({"type": "command", **command.model_dump()})
                async with asyncio.timeout(RECEIPT_SECONDS):
                    receipt = await waiter
            finally:
                self.receipts.pop(command.request_id, None)
            receipt.pop("type", None)
            if "error" in receipt:
                raise RuntimeError(f"Firmware rejected command: {receipt['error']}")
            return receipt

        return DeviceSDK(
            turn.scope,
            turn.id,
            execute,
            lambda: not self.closed and self.turn is turn,
        )

    async def play(self, pcm: bytes, playing: Callable[[], bool]) -> None:
        clock = asyncio.get_running_loop().time
        for offset in range(0, len(pcm), REPLY_FRAME_BYTES):
            if self.closed or not playing():
                return
            chunk = pcm[offset : offset + REPLY_FRAME_BYTES]
            self.playhead = max(self.playhead, clock()) + len(chunk) / (
                SAMPLE_RATE * SAMPLE_BYTES
            )
            await self.send_audio(chunk)
            await asyncio.sleep(max(0, self.playhead - clock() - REPLY_LEAD_SECONDS))

    async def wait_for_playout(self) -> None:
        await asyncio.sleep(max(0, self.playhead - asyncio.get_running_loop().time()))

    async def respond(self, turn: Turn, text: str, tts_timing: list) -> str:
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=4)
        sentences = Sentences()
        streamed = ""
        spoken = ""
        speaking = True
        complete = False

        async def speak(text: str, final: bool) -> None:
            nonlocal spoken, speaking
            if not speaking:
                return
            visible, ended = spoken_part(text, final)
            pieces = sentences.feed(visible[len(spoken) :], final=final or ended)
            spoken = visible
            speaking = not ended
            for piece in pieces:
                await queue.put(piece)

        async def progress(event: dict) -> None:
            nonlocal streamed, complete
            if self.turn is not turn:
                return
            await self.agent_progress(turn, event)
            if event["type"] == "reply.delta":
                streamed += event["delta"]
                await self.publish(turn, "reply", text=streamed, final=False)
                turn.timing.mark("reply_text_published")
                await speak(streamed, final=False)
            elif event["type"] == "answer.complete":
                answer = event["text"]
                if streamed and streamed.strip() != answer.strip():
                    raise RuntimeError("Final agent answer differs from spoken deltas")
                if not streamed:
                    streamed = answer
                    await self.publish(turn, "reply", text=answer, final=False)
                    turn.timing.mark("reply_text_published")
                await speak(streamed, final=True)
                complete = True

        async def generate() -> str:
            turn.timing.mark("agent_requested")
            answer = await self.agent.run(
                text, progress, device_context=agent_context(turn.scope)
            )
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
                            turn.timing.mark("reply_first_enqueue")
                            await self.play(pcm, lambda: self.turn is turn)
            turn.timing.mark("tts_completed")
            await self.wait_for_playout()
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
                        await self.publish(
                            turn, "transcript", text=text.strip(), final=False
                        )
            turn.timing.mark("stt_completed")
            logger.info("Transcription finished for turn %s", turn.id)
            await self.publish(turn, "transcript", text=text.strip(), final=True)
            turn.timing.mark("transcript_published")
            if text.strip():
                stage = "assistant response"
                reply = await self.respond(turn, text.strip(), tts_timing)
                logger.info("Speech played for turn %s", turn.id)
                timing = {
                    "server": turn.timing.snapshot(),
                    "agent": self.agent.timings,
                    "agent_runtime": self.agent.last_run.get("runtime"),
                    "agent_usage": self.agent.last_run.get("usage"),
                    "stt": stt_timing,
                    "tts": {"segments": tts_timing},
                }
                self.remember(
                    turn.id,
                    {"transcript": text.strip(), "reply": reply, "timing": timing},
                )
                fields = {"timing": timing} if self.send_timing else {}
                await self.publish(turn, "reply", text=reply, final=True, **fields)
                self.watch_background()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s failed for turn %s", stage, turn.id)
            await self.publish(
                turn,
                "error",
                stage=stage,
                message=f"{stage.capitalize()} failed. Try another recording.",
            )
        finally:
            turn.finish()
            if self.turn is turn:
                self.turn = None

    async def start_call(self) -> None:
        async with self.turn_lock:
            if self.call is not None:
                logger.warning("Device %s asked for a live call during one", self.id)
                return
            await self._abort()
            voice: asyncio.Queue[bytes] = asyncio.Queue()
            call = LiveCall(
                delegate=self.handoff, speak=voice.put_nowait, send=self.send
            )
            self.call = call
            if self.resampler is not None:
                self.resampler.clear()
            self.call_task = self.spawn(self.converse(call, voice))
        logger.info("Live call %s started for device %s", call.id, self.id)

    async def end_call(self) -> None:
        call = self.call
        if call is None:
            return
        call.hang_up()
        self.spawn(self.drop_call(call))

    async def drop_call(self, call: LiveCall) -> None:
        await asyncio.sleep(HANGUP_SECONDS)
        if self.call is call and self.call_task is not None:
            logger.warning(
                "GPT-Live did not close call %s within %ss; dropping it",
                call.id,
                HANGUP_SECONDS,
            )
            self.call_task.cancel()

    async def converse(self, call: LiveCall, voice: asyncio.Queue[bytes]) -> None:
        player = self.spawn(self.speak_call(call, voice))
        error = None
        try:
            await call.run()
        except asyncio.CancelledError:
            error = "The live call stopped responding."
            raise
        except Exception:
            logger.exception("Live call %s failed", call.id)
            error = "The live call failed. Try again."
        finally:
            player.cancel()
            if self.call is call:
                self.call = None
                self.call_task = None
                self.playhead = 0.0
            ended = {
                "type": "live",
                "state": "ended",
                "call_id": call.id,
                "seconds": round(call.seconds, 1),
            }
            if error:
                ended["error"] = error
            report = call.report()
            self.remember(call.id, report)
            if self.send_timing:
                ended["report"] = report
            await self.send(ended)
            logger.info("Live call %s ended after %.1fs", call.id, call.seconds)

    async def speak_call(self, call: LiveCall, voice: asyncio.Queue[bytes]) -> None:
        pending = b""
        while True:
            pending += await voice.get()
            whole = len(pending) - len(pending) % REPLY_FRAME_BYTES
            if whole:
                await self.play(pending[:whole], lambda: self.call is call)
                pending = pending[whole:]

    def remember(self, key: str, report: dict) -> None:
        self.reports[key] = report
        while len(self.reports) > KEPT_REPORTS:
            self.reports.pop(next(iter(self.reports)))

    async def handoff(self, request: str) -> tuple[str, dict]:
        """Runs one GPT-Live handoff on the agent, with the device scope and
        approvals of a turn of its own."""
        turn = Turn()
        turn.finish()
        self.turn = turn
        try:
            await self.publish(turn, "tool", tool="slate agent")
            reply = await self.agent.run(
                request,
                partial(self.agent_progress, turn),
                device_context=agent_context(turn.scope),
            )
        finally:
            if self.turn is turn:
                self.turn = None
        details = {
            "usage": self.agent.last_run.get("usage"),
            "agent": self.agent.timings,
            "runtime": self.agent.last_run.get("runtime"),
        }
        return spoken_part(reply, final=True)[0].strip(), details

    def watch_background(self) -> None:
        if self.agent.pending and (self.watcher is None or self.watcher.done()):
            self.watcher = self.spawn(self.report_background())

    async def report_background(self) -> None:
        while self.agent.pending:
            finished = await self.agent.background_finished()
            logger.info("Announcing background results %s", sorted(finished))
            if (call := self.call) is not None:
                reply = await self.agent.run(BACKGROUND_PROMPT)
                await call.tell(spoken_part(reply, final=True)[0].strip())
                continue
            while True:
                async with self.turn_lock:
                    if self.closed:
                        return
                    if self.turn is None:
                        turn = Turn(announcement=True)
                        turn.finish()
                        self.turn = turn
                        task = self.turn_task = self.spawn(self.announce(turn))
                        break
                await asyncio.sleep(0.2)
            await asyncio.gather(task, return_exceptions=True)

    async def announce(self, turn: Turn) -> None:
        try:
            reply = await self.respond(turn, BACKGROUND_PROMPT, [])
            await self.publish(turn, "reply", text=reply, final=True)
            self.watch_background()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background announcement failed for turn %s", turn.id)
            await self.publish(
                turn, "error", message="The background result could not be read"
            )
        finally:
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
        self.playhead = 0.0
        if turn is None:
            return
        turn.finish()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.send(
            {
                "type": "cancelled",
                "turn_id": turn.id,
                **({"message": message} if message else {}),
            }
        )

    async def close(self) -> None:
        if self.closed:
            await self.close_done.wait()
            return
        self.closed = True
        if self.turn:
            self.turn.finish()
            self.turn = None
        for waiter in self.receipts.values():
            waiter.cancel()
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        try:
            async with AsyncExitStack() as cleanup:
                cleanup.push_async_callback(self.agent.close)
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.close_done.set()


class VoiceSessions:
    def __init__(self) -> None:
        self.current: VoiceSession | None = None
        self.lock = asyncio.Lock()

    async def connect(self, socket: WebSocket, hello: Hello) -> VoiceSession:
        async with self.lock:
            previous, self.current = self.current, VoiceSession(socket, hello)
            if previous is not None and not previous.closed:
                logger.warning("Device %s replaced by a new connection", previous.id)
                await previous.close()
                await previous.socket.close(4000, "Replaced by a new connection")
            return self.current

    async def close(self) -> None:
        if self.current:
            await self.current.close()
