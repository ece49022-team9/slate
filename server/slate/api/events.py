import asyncio
import itertools
import secrets
from collections import deque
from datetime import UTC, datetime
from typing import Any

import anyio
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.requests import HTTPConnection

Event = dict[str, Any]

router = APIRouter(tags=["events"])

LOCAL_HOSTS = ("127.0.0.1", "::1")
LOCAL_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")

SEND_TIMEOUT = 5.0


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventBus:
    def __init__(self, size: int = 500, queue_size: int = 100) -> None:
        self.buffer: deque[Event] = deque(maxlen=size)
        self.queues: set[asyncio.Queue[Event | None]] = set()
        self.queue_size = queue_size
        self.ids = itertools.count(1)
        self.boot = secrets.token_hex(3)
        self.prefix = f"evt_{self.boot}_"
        self.last_seq = 0
        self.dropped = 0
        self.stats = {"published": 0, "resets": 0, "stalled": 0, "max_depth": 0}

    def envelope(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        device_id: str | None = None,
        session_id: str | None = None,
    ) -> Event:
        self.last_seq = next(self.ids)
        return {
            "v": 1,
            "id": f"{self.prefix}{self.last_seq:010d}",
            "type": event_type,
            "ts": now(),
            "device_id": device_id,
            "session_id": session_id,
            "data": data,
        }

    def publish(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        device_id: str | None = None,
        session_id: str | None = None,
    ) -> Event:
        event = self.envelope(
            event_type, data, device_id=device_id, session_id=session_id
        )
        if len(self.buffer) == self.buffer.maxlen:
            self.dropped = self.seq(self.buffer[0]["id"])
        self.buffer.append(event)
        self.stats["published"] += 1
        for queue in self.queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
                self.stats["resets"] += 1
            else:
                depth = queue.qsize()
                if depth > self.stats["max_depth"]:
                    self.stats["max_depth"] = depth
        return event

    def seq(self, event_id: str) -> int | None:
        if not event_id.startswith(self.prefix):
            return None
        tail = event_id[len(self.prefix) :]
        return int(tail) if tail.isdigit() else None

    def since(self, cursor: str | None) -> list[Event] | None:
        if cursor is None:
            return list(self.buffer)
        seen = self.seq(cursor)
        if seen is None or seen > self.last_seq:
            return None
        if seen < self.dropped:
            return None
        return [event for event in self.buffer if self.seq(event["id"]) > seen]

    def subscribe(self) -> asyncio.Queue[Event | None]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue(self.queue_size)
        self.queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event | None]) -> None:
        self.queues.discard(queue)

    def snapshot(self) -> dict[str, Any]:
        return {
            "boot": self.boot,
            **self.stats,
            "listeners": len(self.queues),
            "buffered": len(self.buffer),
            "buffer_size": self.buffer.maxlen,
            "queue_size": self.queue_size,
        }


def is_local(connection: HTTPConnection) -> bool:
    origin = connection.headers.get("origin")
    return (
        connection.client is not None
        and connection.client.host in LOCAL_HOSTS
        and (origin is None or origin in LOCAL_ORIGINS)
    )


@router.get("/events/stats")
def event_stats(request: Request) -> dict[str, Any]:
    if not is_local(request):
        raise HTTPException(403, "Only the web app on this machine can use this")
    return request.app.state.events.snapshot()


@router.websocket("/ws/events")
async def stream_events(websocket: WebSocket, after: str | None = None) -> None:
    if not is_local(websocket):
        await websocket.close(code=1008)
        return
    bus: EventBus = websocket.app.state.events
    await websocket.accept()
    queue = bus.subscribe()
    backlog = bus.since(after)
    if backlog is None:
        backlog = [bus.envelope("system.resync", {}), *bus.buffer]
    try:
        async with anyio.create_task_group() as group:

            async def send() -> None:
                await send_events(websocket, backlog, queue, bus)
                group.cancel_scope.cancel()

            async def listen() -> None:
                await wait_for_close(websocket)
                group.cancel_scope.cancel()

            group.start_soon(send)
            group.start_soon(listen)
    finally:
        bus.unsubscribe(queue)


async def send_events(
    websocket: WebSocket,
    backlog: list[Event],
    queue: asyncio.Queue[Event | None],
    bus: EventBus | None = None,
) -> None:
    try:
        for event in backlog:
            with anyio.fail_after(SEND_TIMEOUT):
                await websocket.send_json(event)
        while (event := await queue.get()) is not None:
            with anyio.fail_after(SEND_TIMEOUT):
                await websocket.send_json(event)
        await websocket.close(code=1013)
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        if bus is not None:
            bus.stats["stalled"] += 1


async def wait_for_close(websocket: WebSocket) -> None:
    async for _ in websocket.iter_text():
        pass
