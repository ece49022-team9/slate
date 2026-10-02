import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from livekit import rtc
from livekit.agents import Agent as VoiceAgent
from livekit.agents import (
    AgentServer,
    AgentSession,
    ChatMessage,
    ConversationItemAddedEvent,
    JobContext,
    JobRequest,
    SessionUsageUpdatedEvent,
    cli,
)
from livekit.agents.metrics.usage import LLMModelUsage
from livekit.plugins.openai.realtime import GPTLiveDelegation, GPTLiveModel

from slate.agent import Agent
from slate.voice.settings import LIVE_AGENT, WORKER_IDENTITY, VoiceSettings

logger = logging.getLogger("slate.voice.worker")
INSTRUCTIONS = Path(__file__).with_name("live.md").read_text()
BACKEND_CONTEXT = (
    "Slate's voice model will say your answer out loud. The transcript below "
    "comes from speech recognition, so it may have mistakes. Answer the user's "
    "most recent request.\n\n"
)
FAILED_REPLY = "I couldn't finish that. Please try again."


class Handoffs:
    """Answers GPT-Live's delegations with Hermes, one at a time and in order.
    Each request carries only what was said since the previous one."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.words: list[dict[str, Any]] = []
        self.sent = 0
        self.carried = ""
        self.records: list[dict[str, Any]] = []
        self.order = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.seconds = 0.0
        self.ready = False

    def heard(self, event: ConversationItemAddedEvent) -> None:
        item = event.item
        if not isinstance(item, ChatMessage) or item.role not in ("user", "assistant"):
            return
        if text := (item.text_content or "").strip():
            role = "User" if item.role == "user" else "Slate"
            self.words.append(
                {"role": role, "text": text, "at_ns": time.monotonic_ns()}
            )

    def metered(self, event: SessionUsageUpdatedEvent) -> None:
        self.seconds = sum(
            usage.session_duration
            for usage in event.usage.model_usage
            if isinstance(usage, LLMModelUsage)
        )

    def delegate(self, session, delegation: GPTLiveDelegation) -> None:
        record = {"id": delegation.id, "created_ns": time.monotonic_ns()}
        self.records.append(record)
        task = asyncio.create_task(self.answer(session, delegation, record))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def request(self, pending: str) -> str:
        """New speech since the last handoff, plus the user's unfinished turn. That
        turn reaches the chat history later, so it is skipped once there."""
        fresh = self.words[self.sent :]
        self.sent = len(self.words)
        repeat = next(
            (
                index
                for index, part in enumerate(fresh)
                if part["role"] == "User" and part["text"] == self.carried
            ),
            None,
        )
        if repeat is not None:
            fresh = fresh[:repeat] + fresh[repeat + 1 :]
        lines = [f"{part['role']}: {part['text']}" for part in fresh]
        self.carried = pending.strip()
        if self.carried:
            lines.append(f"User: {self.carried}")
        return "\n".join(lines)

    async def answer(self, session, delegation: GPTLiveDelegation, record: dict):
        async with self.order:
            try:
                request = self.request(delegation.pending_transcript)
                record["request"] = request
                if not request:
                    raise ValueError("No speech arrived before this delegation")
                record["agent_requested_ns"] = time.monotonic_ns()
                reply = await self.agent.run(BACKEND_CONTEXT + request)
                record["agent_completed_ns"] = time.monotonic_ns()
                record["agent"] = self.agent.timings
                record["usage"] = self.agent.last_run.get("usage")
                record["reply"] = reply
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("Delegation %s failed", delegation.id)
                record["error"] = type(error).__name__
                reply = FAILED_REPLY
            record["commentary_sent_ns"] = time.monotonic_ns()
            session.append_commentary(reply, delegation_id=delegation.id)

    def report(self) -> dict:
        return {
            "ready": self.ready,
            "delegations": self.records,
            "words": self.words,
            "usage": {"seconds": self.seconds},
            "agent_runtime": self.agent.last_run.get("runtime"),
        }

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.agent.close()


class SlateVoice(VoiceAgent):
    def __init__(self, handoffs: Handoffs) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self.handoffs = handoffs

    async def on_enter(self) -> None:
        session = self.duplex_session
        session.on(
            "delegation_created",
            lambda delegation: self.handoffs.delegate(session, delegation),
        )


livekit = VoiceSettings.from_env()
server = AgentServer(
    ws_url=livekit.url, api_key=livekit.api_key, api_secret=livekit.api_secret
)


async def accept(request: JobRequest) -> None:
    await request.accept(identity=WORKER_IDENTITY)


@server.rtc_session(agent_name=LIVE_AGENT, on_request=accept)
async def live(ctx: JobContext) -> None:
    handoffs = Handoffs(Agent())
    session = AgentSession(llm=GPTLiveModel(delegation="client"))
    session.on("conversation_item_added", handoffs.heard)
    session.on("session_usage_updated", handoffs.metered)
    ctx.add_shutdown_callback(handoffs.close)
    await ctx.connect()

    def device(data: rtc.RpcInvocationData) -> None:
        if not data.caller_identity.startswith("device-"):
            raise rtc.RpcError(1501, "This session belongs to another device")

    async def report(data: rtc.RpcInvocationData) -> str:
        device(data)
        return json.dumps(handoffs.report())

    async def finish(data: rtc.RpcInvocationData) -> str:
        device(data)
        await session.aclose()
        return json.dumps(handoffs.report())

    ctx.room.local_participant.register_rpc_method("live_report", report)
    ctx.room.local_participant.register_rpc_method("live_finish", finish)
    await session.start(agent=SlateVoice(handoffs), room=ctx.room)
    handoffs.ready = True


if __name__ == "__main__":
    cli.run_app(server)
