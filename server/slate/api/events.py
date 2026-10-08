import asyncio
import itertools
from collections import deque
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

Event = dict[str, Any]

router = APIRouter(tags=["events"])

LOCAL_HOSTS = ("127.0.0.1", "::1")
LOCAL_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventBus:
    """Recent events in memory, pushed live to every connected web app."""

    def __init__(self, size: int = 500, queue_size: int = 100) -> None:
        self.buffer: deque[Event] = deque(maxlen=size)
        self.queues: set[asyncio.Queue[Event | None]] = set()
        self.queue_size = queue_size
        self.ids = itertools.count(1)

    def envelope(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        device_id: str | None = None,
        session_id: str | None = None,
    ) -> Event:
        return {
            "v": 1,
            "id": f"evt_{next(self.ids):010d}",
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
        self.buffer.append(event)
        for queue in self.queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # This web app fell behind. Drop its backlog and make it reconnect.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
        return event

    def since(self, cursor: str | None) -> list[Event] | None:
        """Events after cursor, or None if they are no longer in the buffer."""
        if cursor is None:
            return list(self.buffer)
        if not self.buffer:
            return None
        if not self.buffer[0]["id"] <= cursor <= self.buffer[-1]["id"]:
            return None
        return [event for event in self.buffer if event["id"] > cursor]

    def subscribe(self) -> asyncio.Queue[Event | None]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue(self.queue_size)
        self.queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event | None]) -> None:
        self.queues.discard(queue)


def is_local(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    return (
        websocket.client is not None
        and websocket.client.host in LOCAL_HOSTS
        and (origin is None or origin in LOCAL_ORIGINS)
    )


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
    # Send and listen at the same time, so a closed tab or a server reload
    # ends this connection right away instead of leaving it waiting forever.
    sender = asyncio.create_task(send_events(websocket, backlog, queue))
    listener = asyncio.create_task(wait_for_close(websocket))
    try:
        await asyncio.wait({sender, listener}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        bus.unsubscribe(queue)
        for task in (sender, listener):
            task.cancel()
        await asyncio.gather(sender, listener, return_exceptions=True)


async def send_events(
    websocket: WebSocket, backlog: list[Event], queue: asyncio.Queue[Event | None]
) -> None:
    try:
        for event in backlog:
            await websocket.send_json(event)
        while (event := await queue.get()) is not None:
            await websocket.send_json(event)
        await websocket.close(code=1013)
    except WebSocketDisconnect:
        pass


async def wait_for_close(websocket: WebSocket) -> None:
    """The web app never sends anything, so this returns when it disconnects."""
    async for _ in websocket.iter_text():
        pass
