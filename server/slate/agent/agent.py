import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic_ns
from uuid import uuid4

import httpx

from slate.agent.runtime import settings
from slate.board import ROOT

logger = logging.getLogger("slate.agent")
Progress = Callable[[dict], Awaitable[None]]
INSTRUCTIONS = (
    "You are Slate, a personal voice assistant. Use tools when you need them, and "
    "report what they actually returned. Start your final answer with one or two "
    "sentences to say out loud. If the full answer needs more, put the rest after a "
    "line with only ---; Slate shows that part on screen and doesn't say it. Don't "
    "say progress updates or your private reasoning. For work that will take more "
    "than about a minute, like research, browsing several pages, or coding, start "
    "it with delegate_task so it runs in the background, say briefly that you "
    "started it, and end your turn. When its result comes back, tell the user. "
    "Sending messages and spending money stop for the user's approval on their own, "
    "so go ahead with those requests without asking again. If you're missing "
    "something you need, ask one short question."
)
BACKGROUND_PROMPT = "A background task you started has finished. Tell me its result."


class Agent:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        session_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        instructions: str = INSTRUCTIONS,
        key_file: Path = ROOT / ".local/hermes-home/api-key",
    ) -> None:
        self.client = client or httpx.AsyncClient(
            base_url=os.getenv("SLATE_AGENT_URL", "http://127.0.0.1:8642"),
            headers={
                "Authorization": "Bearer "
                + (os.getenv("SLATE_AGENT_KEY") or key_file.read_text().strip())
            },
            timeout=httpx.Timeout(30, read=30),
        )
        cfg = settings()
        self.session_id = session_id
        self.model = model or cfg["model"]
        self.provider = provider or cfg["provider"]
        self.instructions = instructions
        self.run_id: str | None = None
        self.pending: set[str] = set()
        self.last_run: dict = {}
        self._timings: dict[str, int] = {}
        self.lock = asyncio.Lock()

    @property
    def timings(self) -> dict[str, int]:
        return dict(self._timings)

    async def request(self, method: str, path: str, **kwargs) -> dict:
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

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
            if self.session_id is None:
                created = await self.request(
                    "POST", "/api/sessions", json={"title": "Slate " + uuid4().hex}
                )
                self.session_id = created["session"]["id"]
            self._timings["session_ready"] = monotonic_ns()
            payload = {
                "input": message,
                "session_id": self.session_id,
                "instructions": (
                    self.instructions + "\n\n" + device_context
                    if device_context
                    else self.instructions
                ),
                "provider": self.provider,
                "model": self.model,
            }
            key = uuid4().hex
            creation = asyncio.create_task(self.create_run(payload, key))
            try:
                created = await asyncio.shield(creation)
                self.run_id = created["run_id"]
                self._timings["admitted"] = monotonic_ns()
                logger.info(
                    "Run %s started: session=%s provider=%s model=%s",
                    self.run_id,
                    self.session_id,
                    self.provider,
                    self.model,
                )
                result = await self.follow(progress)
                self.last_run = result
                if result["status"] != "completed":
                    raise RuntimeError(
                        f"Hermes run {result['status']}: {result.get('error', '')}"
                    )
                runtime = result.get("runtime", {})
                if (runtime.get("model"), runtime.get("provider")) != (
                    self.model,
                    self.provider,
                ):
                    raise RuntimeError(f"Hermes changed the model route: {runtime}")
                self.session_id = result.get("session_id") or self.session_id
                text = (result.get("output") or "").strip()
                if not text:
                    raise RuntimeError("Hermes completed without a spoken reply")
                if progress:
                    await progress({"type": "answer.complete", "text": text})
                return text
            except BaseException:
                try:
                    async with asyncio.timeout(5):
                        if self.run_id is None:
                            try:
                                async with asyncio.timeout(1):
                                    receipt = await asyncio.shield(creation)
                            except (TimeoutError, httpx.TransportError):
                                receipt = await self.request(
                                    "POST",
                                    "/v1/runs",
                                    json=payload,
                                    headers={"Idempotency-Key": key},
                                )
                            self.run_id = receipt["run_id"]
                            self._timings.setdefault("admitted", monotonic_ns())
                        if self.run_id:
                            await self.request("POST", f"/v1/runs/{self.run_id}/stop")
                except Exception:
                    logger.exception(
                        "Could not recover or stop Hermes run: key=%s run=%s",
                        key,
                        self.run_id,
                    )
                raise
            finally:
                if not creation.done():
                    creation.cancel()
                await asyncio.gather(creation, return_exceptions=True)
                self.run_id = None
                self._timings["completed"] = monotonic_ns()

    async def create_run(self, payload: dict, key: str) -> dict:
        for attempt in range(2):
            try:
                return await self.request(
                    "POST",
                    "/v1/runs",
                    json=payload,
                    headers={"Idempotency-Key": key},
                )
            except httpx.TransportError:
                if attempt:
                    raise
        raise AssertionError("Run admission exhausted without a result")

    async def follow(self, progress: Progress | None) -> dict:
        sequence = -1
        while True:
            try:
                async with self.client.stream(
                    "GET",
                    f"/v1/runs/{self.run_id}/events",
                    headers={"Last-Event-ID": str(sequence)},
                ) as response:
                    response.raise_for_status()
                    data: list[str] = []
                    event_type = ""
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif not line and data:
                            event = json.loads("\n".join(data))
                            data.clear()
                            kind = event_type or event.get("event", "")
                            event_type = ""
                            event_sequence = event.get("seq", sequence + 1)
                            if event_sequence <= sequence:
                                continue
                            sequence = event_sequence
                            observed = monotonic_ns()
                            self._timings.setdefault("first_event", observed)
                            if (
                                kind == "message.delta"
                                and isinstance(event.get("delta"), str)
                                and event["delta"].strip()
                            ):
                                self._timings.setdefault("first_text", observed)
                                self._timings["last_text"] = observed
                            if (
                                kind == "subagent.start"
                                and str(event.get("depth", 0)) == "0"
                                and event.get("delegation_id")
                            ):
                                self.pending.add(event["delegation_id"])
                            if kind in (
                                "run.completed",
                                "run.failed",
                                "run.cancelled",
                                "run.interrupted",
                            ):
                                self._timings.setdefault("terminal", observed)
                            logger.info("Run %s: %s", self.run_id, kind)
                            if progress:
                                await progress({**event, "type": kind})
            except httpx.TransportError:
                logger.warning("Run %s stream disconnected; resuming", self.run_id)
            try:
                result = await self.request("GET", f"/v1/runs/{self.run_id}")
                if result["status"] in (
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                ):
                    self._timings.setdefault("terminal", monotonic_ns())
                    return result
            except httpx.TransportError:
                logger.warning("Run %s status unavailable; retrying", self.run_id)
            await asyncio.sleep(0.5)

    async def background_finished(self) -> set[str]:
        while True:
            history = await self.request(
                "GET",
                f"/api/sessions/{self.session_id}/messages",
                params={"inline_images": "false"},
            )
            finished = {
                delegation
                for delegation in self.pending
                for row in history["data"]
                if row.get("display_kind") == "async_delegation_complete"
                and delegation in str(row.get("content", ""))
            }
            if finished:
                self.pending -= finished
                logger.info("Background delegations finished: %s", sorted(finished))
                return finished
            await asyncio.sleep(2)

    async def approve(self, run_id: str, request_id: str, choice: str) -> None:
        if not self.run_id or run_id != self.run_id:
            raise ValueError("This agent run has ended")
        if choice not in ("once", "deny") or not request_id:
            raise ValueError("Choose once or deny for this exact approval request")
        await self.request(
            "POST",
            f"/v1/runs/{run_id}/approval",
            json={"choice": choice, "request_id": request_id},
        )

    async def close(self) -> None:
        await self.client.aclose()
