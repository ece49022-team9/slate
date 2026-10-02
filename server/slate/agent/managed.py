import asyncio
import logging
from time import monotonic_ns
from uuid import uuid4

from openai import AsyncOpenAI, NotFoundError

from slate.agent.agent import INSTRUCTIONS, Progress
from slate.agent.runtime import settings

logger = logging.getLogger("slate.agent.managed")


class ManagedAgent:
    def __init__(
        self,
        *,
        client: AsyncOpenAI | None = None,
        session_id: str | None = None,
        model: str | None = None,
        instructions: str = INSTRUCTIONS,
        browser: bool = False,
        timeout: float = 300,
    ) -> None:
        self.client = client or AsyncOpenAI(max_retries=0)
        self.session_id = session_id
        self.model = model or settings()["model"]
        self.provider = "openai-managed"
        self.instructions = instructions
        self.browser = browser
        self.timeout = timeout
        self.run_id: str | None = None
        self.last_run: dict = {}
        self._timings: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self._pending: dict[str, dict] = {}
        self._active_task: asyncio.Task | None = None
        self._closed = False
        self._needs_cleanup = False
        self._observed_model: str | None = None

    @property
    def timings(self) -> dict[str, int]:
        return dict(self._timings)

    async def run(
        self,
        message: str,
        progress: Progress | None = None,
        *,
        device_context: str | None = None,
    ) -> str:
        requested = monotonic_ns()
        async with self.lock:
            self._timings = {
                "requested": requested,
                "lock_acquired": monotonic_ns(),
            }
            if self._closed:
                raise RuntimeError("This managed agent has closed")
            if not message.strip():
                raise ValueError("Agent input must not be empty")
            if device_context:
                message = device_context + "\n\nUser request: " + message
            if self._needs_cleanup:
                await self._discard_session()
                if self._needs_cleanup:
                    raise RuntimeError(
                        "Managed session cleanup must succeed before reuse"
                    )
            self._active_task = asyncio.current_task()
            self.last_run = {}
            try:
                async with asyncio.timeout(self.timeout):
                    sessions = self.client.beta.agents.sessions
                    if self.session_id is None:
                        environment = {"type": "none"}
                        agent = {"model": self.model, "instructions": self.instructions}
                        if self.browser:
                            environment = {
                                "type": "openai_hosted",
                                "desktop": {"enabled": True},
                                "network": {"access": "enabled"},
                            }
                            agent["tools"] = [
                                {"type": "computer_use", "include_screenshots": True}
                            ]
                        stream = await sessions.create(
                            environment=environment,
                            agent=agent,
                            input=message,
                            stream=True,
                            timeout=self.timeout,
                        )
                        self._timings["session_ready"] = monotonic_ns()
                    else:
                        session = await sessions.retrieve(self.session_id)
                        self._verify_model(session.agent.model)
                        self._timings["session_ready"] = monotonic_ns()
                        stream = sessions.stream(
                            self.session_id, input=message, timeout=self.timeout
                        )
                    self._timings["admitted"] = monotonic_ns()
                    stream.with_result_collection()
                    terminal_turn = False
                    final_items: set[str] = set()
                    async with stream:
                        async for event in stream:
                            body = event.model_dump(mode="json")
                            kind = body["type"]
                            observed = monotonic_ns()
                            self._timings.setdefault("first_event", observed)
                            if kind == "agent.session.turn.output_text.delta" and (
                                body.get("turn_id") in (None, self.run_id)
                                and body["delta"].strip()
                            ):
                                self._timings.setdefault("first_text", observed)
                                self._timings["last_text"] = observed
                            if kind == "agent.session.created":
                                self.session_id = body["session"]["id"]
                                self._verify_model(body["session"]["agent"]["model"])
                            elif kind == "agent.session.turn.created":
                                if body["turn"].get("subagent_id") is None:
                                    self.run_id = body["turn_id"]
                            elif (
                                kind
                                in (
                                    "agent.session.turn.completed",
                                    "agent.session.turn.failed",
                                    "agent.session.turn.cancelled",
                                )
                                and body["turn_id"] == self.run_id
                            ):
                                terminal_turn = True
                                self._timings.setdefault("terminal", observed)
                            elif kind == "agent.session.turn.item.added":
                                item = body["item"]
                                if (
                                    self.run_id
                                    and item.get("turn_id") == self.run_id
                                    and item["type"] == "message"
                                    and item.get("role") == "assistant"
                                    and item.get("phase") == "final_answer"
                                    and item.get("id")
                                ):
                                    final_items.add(item["id"])
                            if kind == "agent.session.failed":
                                self._timings.setdefault("terminal", observed)
                            await self._progress(body, progress)
                            if (
                                progress
                                and kind == "agent.session.turn.output_text.delta"
                                and body.get("turn_id") in (None, self.run_id)
                                and body["item_id"] in final_items
                                and body["delta"]
                            ):
                                await progress(
                                    {"type": "reply.delta", "delta": body["delta"]}
                                )
                            if kind == "agent.session.failed" or (
                                kind == "agent.session.idle" and terminal_turn
                            ):
                                break
                        result = await stream.get_final_result()
                    self.session_id = result.session_id
                    text = result.output_text.strip()
                    if not text:
                        raise RuntimeError(
                            "Managed agent completed without a spoken reply"
                        )
                    self.last_run = {
                        "run_id": result.turn_id,
                        "session_id": result.session_id,
                        "status": result.turn.status,
                        "output": text,
                        "runtime": {
                            "model": self._observed_model,
                            "provider": self.provider,
                        },
                        "usage": result.turn.usage.model_dump(mode="json")
                        if result.turn.usage is not None
                        else None,
                    }
                    if progress:
                        await progress({"type": "answer.complete", "text": text})
                    return text
            except BaseException as error:
                self.last_run = {
                    "run_id": self.run_id,
                    "session_id": self.session_id,
                    "status": "cancelled"
                    if isinstance(error, asyncio.CancelledError)
                    else "failed",
                    "error_type": type(error).__name__,
                    "runtime": {
                        "model": self._observed_model,
                        "provider": self.provider,
                    },
                }
                logger.warning(
                    "Managed turn %s failed: session=%s error=%s",
                    self.run_id,
                    self.session_id,
                    type(error).__name__,
                )
                await self._discard_session(cancel=True)
                raise
            finally:
                self.run_id = None
                self._pending.clear()
                self._active_task = None
                self._timings["completed"] = monotonic_ns()

    def _verify_model(self, model: str) -> None:
        self._observed_model = model
        if model != self.model:
            raise RuntimeError(f"Managed agent changed the model route to {model}")

    async def _progress(self, event: dict, progress: Progress | None) -> None:
        kind = event["type"]
        if kind == "agent.session.requires_action":
            self._pending = {
                action["request_id"]: action
                for action in event["session"]["required_actions"]
                if action["type"] == "computer_use_approval_request"
                and action["turn_id"] == self.run_id
            }
            if not self._pending or progress is None:
                raise RuntimeError(
                    "Managed agent requires an unsupported external action"
                )
        elif kind in ("agent.session.in_progress", "agent.session.idle"):
            self._pending.clear()
        if progress:
            await progress(event)
            if kind == "agent.session.requires_action":
                for request_id, action in list(self._pending.items()):
                    request = action["request"]
                    origin = request.get("origin", request.get("credential_origin", ""))
                    await progress(
                        {
                            "type": "approval.request",
                            "run_id": self.run_id,
                            "request_id": request_id,
                            "command": f"Managed browser {request['type']}: {origin}",
                            "request": request,
                            "source_type": kind,
                        }
                    )
            elif kind == "agent.session.turn.item.added":
                item = event["item"]
                if item["type"].endswith("_call"):
                    await progress(
                        {
                            "type": "tool.started",
                            "run_id": self.run_id,
                            "tool": item["type"],
                            "item": item,
                            "source_type": kind,
                        }
                    )

    async def approve(self, run_id: str, request_id: str, choice: str) -> None:
        if not self.run_id or run_id != self.run_id or not self.session_id:
            raise ValueError("This agent run has ended")
        if choice not in ("once", "deny") or request_id not in self._pending:
            raise ValueError("Choose once or deny for this exact approval request")
        action = self._pending[request_id]
        request_type = action["request"]["type"]
        if request_type == "browser_origin_access":
            response = {
                "type": request_type,
                "decision": "approve" if choice == "once" else "deny",
            }
        elif request_type == "browser_authentication" and choice == "deny":
            response = {"type": request_type, "action": "cancel"}
        else:
            raise ValueError(
                "Browser sign-in needs a credential form; choose deny to cancel"
            )
        await self.client.beta.agents.sessions.events.create(
            self.session_id,
            events=[
                {
                    "type": "agent.session.input.computer_use_approval_request_result",
                    "request_id": request_id,
                    "response": response,
                }
            ],
            idempotency_key=uuid4().hex,
            timeout=10,
        )
        self._pending.pop(request_id, None)

    async def _discard_session(self, *, cancel: bool = False) -> None:
        if self.session_id is None:
            return
        self._needs_cleanup = True
        sessions = self.client.beta.agents.sessions
        if cancel:
            try:
                async with asyncio.timeout(3):
                    await sessions.events.create(
                        self.session_id,
                        events=[{"type": "agent.session.input.cancel"}],
                        idempotency_key=uuid4().hex,
                        timeout=3,
                    )
            except Exception as error:
                logger.warning(
                    "Managed session %s cancellation failed: %s",
                    self.session_id,
                    type(error).__name__,
                )
        try:
            async with asyncio.timeout(5):
                await sessions.delete(self.session_id, timeout=5)
        except NotFoundError:
            self.session_id = None
            self._needs_cleanup = False
        except Exception as error:
            logger.warning(
                "Managed session %s deletion failed: %s",
                self.session_id,
                type(error).__name__,
            )
        else:
            self.session_id = None
            self._needs_cleanup = False

    async def close(self) -> None:
        self._closed = True
        task = self._active_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.lock:
            await self._discard_session()
            if self._needs_cleanup:
                raise RuntimeError(
                    f"Managed session {self.session_id} could not be deleted"
                )
            await self.client.close()
