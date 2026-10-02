import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

import httpx

from slate.agent.runtime import settings
from slate.board import ROOT

logger = logging.getLogger("slate.agent")
Progress = Callable[[dict], Awaitable[None]]
INSTRUCTIONS = (
    "You are Slate, a personal voice assistant. Use tools when needed, and report "
    "their actual results. Keep the final answer short and natural to speak aloud, "
    "usually at most two sentences. Do not speak progress messages or private "
    "reasoning. Ask before sending messages, purchasing, or changing accounts."
)


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
        self.last_run: dict = {}
        self.lock = asyncio.Lock()

    async def request(self, method: str, path: str, **kwargs) -> dict:
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    async def run(self, message: str, progress: Progress | None = None) -> str:
        async with self.lock:
            if self.session_id is None:
                created = await self.request(
                    "POST", "/api/sessions", json={"title": "Slate " + uuid4().hex}
                )
                self.session_id = created["session"]["id"]
            payload = {
                "input": message,
                "session_id": self.session_id,
                "instructions": self.instructions,
                "provider": self.provider,
                "model": self.model,
            }
            key = uuid4().hex
            creation = asyncio.create_task(self.create_run(payload, key))
            try:
                created = await asyncio.shield(creation)
                self.run_id = created["run_id"]
                logger.info(
                    "Run %s started: session=%s provider=%s model=%s",
                    self.run_id,
                    self.session_id,
                    self.provider,
                    self.model,
                )
                async with asyncio.timeout(300):
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
                    return result
            except httpx.TransportError:
                logger.warning("Run %s status unavailable; retrying", self.run_id)
            await asyncio.sleep(0.5)

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
