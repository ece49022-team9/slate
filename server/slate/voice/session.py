import asyncio
import json
import logging
import os
from collections.abc import Callable, Coroutine
from contextlib import aclosing
from functools import partial
from typing import Any, Literal
from uuid import uuid4

import httpx
import numpy as np
import soxr
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from slate.access.credentials import Credential
from slate.agent import create_agent
from slate.agent.agent import BACKGROUND_PROMPT
from slate.device import DeviceCommand, DeviceSDK, agent_context
from slate.events import EventBus
from slate.tasks.machine import InvalidTransition, State
from slate.tasks.manager import TaskManager
from slate.tasks.store import ApprovalClosed, TaskNotFound
from slate.voice.audio import SAMPLE_BYTES, SAMPLE_RATE
from slate.voice.client import speak_stream, transcribe_stream
from slate.voice.live import LiveCall
from slate.voice.reply import Sentences
from slate.voice.turn import Turn

logger = logging.getLogger("slate.voice.session")
SPOKEN_END = "\n---"
REPLY_FRAME_BYTES = SAMPLE_RATE * SAMPLE_BYTES // 50
REPLY_LEAD_SECONDS = 1.0
CALL_LEAD_SECONDS = 0.2
RECEIPT_SECONDS = 5
HANGUP_SECONDS = 5
KEPT_REPORTS = 50
APPROVAL_SECONDS = 120
SUMMARY_CHARS = 200
SLATE_RUN = "slate"
PROFILE = os.getenv("SLATE_PROFILE") == "1"


class Hello(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["hello"]
    rate: int = Field(default=16_000, ge=8_000, le=48_000)
    speaker_rate: int = Field(default=SAMPLE_RATE, ge=8_000, le=48_000)
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
    def __init__(
        self,
        socket: WebSocket,
        hello: Hello,
        events: EventBus | None = None,
        tasks: TaskManager | None = None,
        device_id: str | None = None,
    ) -> None:
        self.socket = socket
        self.events = events
        self.task_manager = tasks
        self.device_id = device_id
        self.turn_tasks: dict[str, str] = {}
        self.approvals: dict[tuple[str, str], str] = {}
        self.background: dict[str, str] = {}
        self.tools: dict[str, list[tuple[str, str]]] = {}
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
        self.speaker = (
            None
            if hello.speaker_rate == SAMPLE_RATE
            else soxr.ResampleStream(SAMPLE_RATE, hello.speaker_rate, 1, dtype="int16")
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

    def report(self, state: str, *, online: bool = True) -> None:
        if self.events is None:
            return
        self.events.publish(
            "device.status",
            {
                "online": online,
                "state": state,
                "battery_percent": None,
                "wifi_rssi": None,
                "firmware_version": None,
            },
            device_id=self.device_identity,
        )

    def step(
        self, turn: Turn, step_id: str, kind: str, title: str, status: str = "done"
    ) -> None:
        if self.events is None or not title:
            return
        self.events.publish(
            "agent.step",
            {"step_id": step_id, "kind": kind, "status": status, "title": title},
            device_id=self.device_identity,
            session_id=turn.id,
        )

    def track(self, turn: Turn, request: str) -> None:
        self.step(turn, "request", "request", request)
        if self.task_manager is None:
            return
        task = self.task_manager.create(
            request[:SUMMARY_CHARS], kind="voice", session_id=turn.id
        )
        self.task_manager.start(task.id)
        self.turn_tasks[turn.id] = task.id

    def settle(self, turn: Turn, outcome: str, detail: str) -> None:
        for step_id, tool in self.tools.pop(turn.id, []):
            status = "done" if outcome == "succeed" else "failed"
            self.step(turn, step_id, "tool", tool, status)
        task_id = self.turn_tasks.pop(turn.id, None)
        if task_id is None or self.task_manager is None:
            return
        try:
            if outcome == "succeed":
                self.task_manager.succeed(task_id, detail[:SUMMARY_CHARS])
            elif outcome == "fail":
                self.task_manager.fail(task_id, detail)
            else:
                self.task_manager.cancel(task_id, actor="device")
        except InvalidTransition as error:
            if error.state is not State.AWAITING_APPROVAL:
                logger.info("Task %s had already finished", task_id)
                return
            logger.warning("Agent run ended while task %s waited for approval", task_id)
            self.task_manager.cancel(task_id, actor="agent")
        except TaskNotFound:
            logger.info("Task %s is gone", task_id)

    async def task_cancelled(self, task_id: str) -> None:
        turn = self.turn
        if turn is not None and self.turn_tasks.get(turn.id) == task_id:
            self.turn_tasks.pop(turn.id)
            await self.abort("Cancelled from the web app")

    def ask(self, turn: Turn, event: dict) -> str | None:
        task_id = self.turn_tasks.get(turn.id)
        if self.task_manager is None or task_id is None:
            return None
        try:
            approval = self.task_manager.request_approval(
                task_id,
                event.get("command", "Tool permission requested"),
                timeout=APPROVAL_SECONDS,
            )
        except InvalidTransition as error:
            logger.warning("Approval for task %s not tracked: %s", task_id, error)
            return None
        key = (event["run_id"], event["request_id"])
        self.approvals[key] = approval.id
        self.spawn(self.relay(turn, approval.id, *key))
        return approval.id

    async def relay(
        self, turn: Turn, approval_id: str, run_id: str, request_id: str
    ) -> None:
        assert self.task_manager is not None
        try:
            approval = await self.task_manager.wait_for_decision(approval_id)
        finally:
            self.approvals.pop((run_id, request_id), None)
        await self.publish(
            turn,
            "approval_resolved",
            run_id=run_id,
            request_id=request_id,
            approval_id=approval_id,
            status=approval.status,
            decided_by=approval.decided_by,
        )
        choice = "once" if approval.status == "approved" else "deny"
        try:
            await self.agent.approve(run_id, request_id, choice)
        except (ValueError, httpx.HTTPError) as error:
            logger.info("Approval %s was not delivered: %s", approval_id, error)

    async def ask_for_credential(
        self, turn: Turn, task_id: str, credential: Credential, purpose: str
    ) -> bool:
        assert self.task_manager is not None
        summary = (
            f"Use your {credential.service} {credential.kind} "
            f"({credential.label}) to {purpose}"
        )
        try:
            approval = self.task_manager.request_approval(
                task_id,
                summary,
                timeout=APPROVAL_SECONDS,
                credentials=[credential.id],
            )
        except InvalidTransition as error:
            logger.warning("Credential approval for %s refused: %s", task_id, error)
            return False
        key = (SLATE_RUN, approval.id)
        self.approvals[key] = approval.id
        try:
            await self.publish(
                turn,
                "approval",
                run_id=SLATE_RUN,
                request_id=approval.id,
                command=summary,
                approval_id=approval.id,
            )
            decided = await self.task_manager.wait_for_decision(approval.id)
        finally:
            self.approvals.pop(key, None)
        await self.publish(
            turn,
            "approval_resolved",
            run_id=SLATE_RUN,
            request_id=approval.id,
            approval_id=approval.id,
            status=decided.status,
            decided_by=decided.decided_by,
        )
        return decided.status == "approved"

    @property
    def device_identity(self) -> str:
        return self.device_id or f"device-{self.id}"

    async def serve(self) -> None:
        self.report("idle")
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
        if self.closed or not pcm:
            return
        async with self.send_lock:
            await self.socket.send_bytes(pcm)

    def for_speaker(self, pcm: bytes, last: bool = False) -> bytes:
        if self.speaker is None:
            return pcm
        out = self.speaker.resample_chunk(np.frombuffer(pcm, dtype="<i2"), last=last)
        if last:
            self.speaker.clear()
        return out.tobytes()

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
            if self.speaker is not None:
                self.speaker.clear()
            self.turn_task = self.spawn(self.transcribe(turn))
        self.report("listen")
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
        self.report("transcribe")

    async def approve(self, message: dict) -> None:
        try:
            if self.turn is None or message["turn_id"] != self.turn.id:
                raise ValueError("This recording has ended")
            if message["choice"] not in ("once", "deny"):
                raise ValueError("Choose once or deny for this exact approval request")
            approval_id = self.approvals.get((message["run_id"], message["request_id"]))
            if approval_id is not None and self.task_manager is not None:
                try:
                    self.task_manager.decide(
                        approval_id, message["choice"] == "once", actor="device"
                    )
                except ApprovalClosed:
                    raise ValueError("This approval was already answered") from None
                return
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
            approval_id = self.ask(turn, event)
            await self.publish(
                turn,
                "approval",
                run_id=event["run_id"],
                request_id=event["request_id"],
                command=event.get("command", "Tool permission requested"),
                **({"approval_id": approval_id} if approval_id else {}),
            )
        elif event["type"] == "tool.started":
            tool = event.get("tool", "tool")
            opened = self.tools.setdefault(turn.id, [])
            step_id = f"tool-{len(opened) + 1}"
            opened.append((step_id, tool))
            self.step(turn, step_id, "tool", tool, "running")
            await self.publish(turn, "tool", tool=tool)
        elif event["type"] == "tool.completed":
            opened = self.tools.get(turn.id, [])
            tool = event.get("tool", "tool")
            for index, (step_id, name) in enumerate(opened):
                if name == tool:
                    del opened[index]
                    self.step(turn, step_id, "tool", tool)
                    break
        elif (
            event["type"] == "subagent.start"
            and str(event.get("depth", 0)) == "0"
            and event.get("delegation_id")
            and self.task_manager is not None
        ):
            task = self.task_manager.create(
                event.get("goal") or "Background work",
                kind="background",
                session_id=turn.id,
            )
            self.task_manager.start(task.id)
            self.background[event["delegation_id"]] = task.id

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

    async def play(
        self, pcm: bytes, playing: Callable[[], bool], lead: float = REPLY_LEAD_SECONDS
    ) -> None:
        clock = asyncio.get_running_loop().time
        for offset in range(0, len(pcm), REPLY_FRAME_BYTES):
            if self.closed or not playing():
                return
            chunk = pcm[offset : offset + REPLY_FRAME_BYTES]
            self.playhead = max(self.playhead, clock()) + len(chunk) / (
                SAMPLE_RATE * SAMPLE_BYTES
            )
            await self.send_audio(self.for_speaker(chunk))
            await asyncio.sleep(max(0, self.playhead - clock() - lead))

    async def wait_for_playout(self) -> None:
        await asyncio.sleep(max(0, self.playhead - asyncio.get_running_loop().time()))

    async def respond(self, turn: Turn, text: str, tts_timing: list) -> str:
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=4)
        audio: asyncio.Queue[bytes | None] = asyncio.Queue()
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
                            await audio.put(pcm)
                            turn.timing.mark("reply_first_enqueue")
            turn.timing.mark("tts_completed")
            await audio.put(None)

        async def playback() -> None:
            while (pcm := await audio.get()) is not None:
                await self.play(pcm, lambda: self.turn is turn)
            if self.turn is turn:
                await self.send_audio(self.for_speaker(b"", last=True))
            await self.wait_for_playout()
            turn.timing.mark("reply_playout_done")

        async with asyncio.TaskGroup() as tasks:
            answer = tasks.create_task(generate())
            tasks.create_task(synthesize())
            tasks.create_task(playback())
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
                self.track(turn, text.strip())
                self.report("respond")
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
                self.step(turn, "response", "response", reply)
                self.settle(turn, "succeed", reply)
                self.watch_background()
            self.report("idle")
        except asyncio.CancelledError:
            self.settle(turn, "cancel", "Cancelled")
            raise
        except Exception:
            logger.exception("%s failed for turn %s", stage, turn.id)
            self.settle(turn, "fail", f"{stage.capitalize()} failed")
            self.report("error")
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
                await self.play(
                    pending[:whole], lambda: self.call is call, CALL_LEAD_SECONDS
                )
                pending = pending[whole:]

    def remember(self, key: str, report: dict) -> None:
        self.reports[key] = report
        while len(self.reports) > KEPT_REPORTS:
            self.reports.pop(next(iter(self.reports)))

    async def handoff(self, request: str) -> tuple[str, dict]:
        turn = Turn()
        turn.finish()
        self.turn = turn
        self.track(turn, request)
        try:
            await self.publish(turn, "tool", tool="slate agent")
            reply = await self.agent.run(
                request,
                partial(self.agent_progress, turn),
                device_context=agent_context(turn.scope),
            )
            self.settle(turn, "succeed", reply)
        except asyncio.CancelledError:
            self.settle(turn, "cancel", "Cancelled")
            raise
        except Exception:
            self.settle(turn, "fail", "Assistant response failed")
            raise
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
            for delegation in finished:
                task_id = self.background.pop(delegation, None)
                if task_id is not None and self.task_manager is not None:
                    try:
                        self.task_manager.succeed(task_id, "Background work finished")
                    except (InvalidTransition, TaskNotFound):
                        logger.info("Background task %s had already finished", task_id)
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
        self.report("idle")
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
        self.report("idle", online=False)
        if self.turn:
            self.turn.finish()
            self.turn = None
        for waiter in self.receipts.values():
            waiter.cancel()
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        try:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await self.agent.close()
        finally:
            self.close_done.set()


class VoiceSessions:
    def __init__(
        self, events: EventBus | None = None, tasks: TaskManager | None = None
    ) -> None:
        self.events = events
        self.task_manager = tasks
        self.current: VoiceSession | None = None
        self.lock = asyncio.Lock()

    async def connect(
        self, socket: WebSocket, hello: Hello, device_id: str | None = None
    ) -> VoiceSession:
        async with self.lock:
            previous, self.current = (
                self.current,
                VoiceSession(socket, hello, self.events, self.task_manager, device_id),
            )
            if previous is not None and not previous.closed:
                logger.warning("Device %s replaced by a new connection", previous.id)
                await previous.close()
                try:
                    await previous.socket.close(4000, "Replaced by a new connection")
                except (WebSocketDisconnect, RuntimeError):
                    logger.info("Device %s socket was already gone", previous.id)
            return self.current

    async def close(self) -> None:
        if self.current:
            await self.current.close()
