import asyncio
import itertools
import secrets
from collections import deque
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.requests import HTTPConnection

Event = dict[str, Any]

router = APIRouter(tags=["events"])

LOCAL_HOSTS = ("127.0.0.1", "::1")
LOCAL_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")

# Longest a single send may block. A web app that stops reading (frozen tab,
# dead Wi-Fi) fills the TCP buffers, and then send() waits forever. After this
# long we give up on it so it cannot hold a connection open indefinitely.
SEND_TIMEOUT = 5.0


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventBus:
    """Recent events in memory, pushed live to every connected web app.

    Delivery rule: a web app that reconnects with the last id it saw gets
    every event after it, in order, with no gaps and no repeats. If that is no
    longer possible (the events left the buffer, or the server restarted) it
    gets system.resync first, so it never misses events without knowing.

    Event ids are evt_<boot>_<seq>. boot changes on every server start, so a
    cursor from before a restart is always recognized as stale.
    """

    def __init__(self, size: int = 500, queue_size: int = 100) -> None:
        self.buffer: deque[Event] = deque(maxlen=size)
        self.queues: set[asyncio.Queue[Event | None]] = set()
        self.queue_size = queue_size
        self.ids = itertools.count(1)
        self.boot = secrets.token_hex(3)
        self.prefix = f"evt_{self.boot}_"
        self.stats = {"published": 0, "resets": 0, "stalled": 0, "max_depth": 0}

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
            "id": f"{self.prefix}{next(self.ids):010d}",
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
        self.stats["published"] += 1
        for queue in self.queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # This web app fell behind. Drop its backlog and make it
                # reconnect; it resumes from its cursor out of self.buffer.
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
        """The sequence number of an id from this boot, else None."""
        if not event_id.startswith(self.prefix):
            return None
        tail = event_id[len(self.prefix) :]
        return int(tail) if tail.isdigit() else None

    def since(self, cursor: str | None) -> list[Event] | None:
        """Events after cursor, or None if some of them are gone."""
        if cursor is None:
            return list(self.buffer)
        seen = self.seq(cursor)
        if seen is None or not self.buffer:
            # Another boot, a malformed id, or nothing kept since the restart.
            return None
        first = self.seq(self.buffer[0]["id"])
        last = self.seq(self.buffer[-1]["id"])
        # seen == first - 1 is fine: the web app already has the event just
        # before the oldest one we kept, so nothing is missing.
        if not first - 1 <= seen <= last:
            return None
        return list(itertools.islice(self.buffer, seen - first + 1, None))

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
        raise HTTPException(403, "only the local web app can use this")
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
    # Send and listen at the same time, so a closed tab or a server reload
    # ends this connection right away instead of leaving it waiting forever.
    sender = asyncio.create_task(send_events(websocket, backlog, queue, bus))
    listener = asyncio.create_task(wait_for_close(websocket))
    try:
        await asyncio.wait({sender, listener}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        bus.unsubscribe(queue)
        for task in (sender, listener):
            task.cancel()
        await asyncio.gather(sender, listener, return_exceptions=True)


async def send_events(
    websocket: WebSocket,
    backlog: list[Event],
    queue: asyncio.Queue[Event | None],
    bus: EventBus | None = None,
) -> None:
    try:
        for event in backlog:
            await asyncio.wait_for(websocket.send_json(event), SEND_TIMEOUT)
        while (event := await queue.get()) is not None:
            await asyncio.wait_for(websocket.send_json(event), SEND_TIMEOUT)
        # 1013 "try again later": the web app fell behind and should reconnect
        # with its cursor right away.
        await websocket.close(code=1013)
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        # The web app stopped reading. Returning ends the handler, which
        # closes the connection; a close frame would only block too.
        if bus is not None:
            bus.stats["stalled"] += 1


async def wait_for_close(websocket: WebSocket) -> None:
    """The web app never sends anything, so this returns when it disconnects."""
    async for _ in websocket.iter_text():
        pass
