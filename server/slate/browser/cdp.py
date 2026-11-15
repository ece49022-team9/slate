import asyncio
import itertools
import json
from collections.abc import Callable

OWN_IDS = 2**30
ASK_SECONDS = 3.0


class CdpError(RuntimeError):
    pass


class Devtools:
    def __init__(self, send: Callable[[str], None], first_id: int = OWN_IDS) -> None:
        self.send = send
        self.first_id = first_id
        self.ids = itertools.count(first_id)
        self.pending: dict[int, tuple[str, asyncio.Future]] = {}

    def ask(
        self, method: str, params: dict | None = None, session: str = ""
    ) -> asyncio.Future:
        number = next(self.ids)
        message = {"id": number, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(settle)
        self.pending[number] = (session, future)
        self.send(json.dumps(message))
        return future

    async def call(
        self,
        method: str,
        params: dict | None = None,
        session: str = "",
        timeout: float = ASK_SECONDS,
    ) -> dict:
        return await answer(self.ask(method, params, session), method, timeout)

    def heard(self, message: dict) -> bool:
        number = message.get("id")
        if not isinstance(number, int) or number < self.first_id:
            return False
        entry = self.pending.pop(number, None)
        if entry is None:
            return True
        future = entry[1]
        if future.done():
            return True
        if "error" in message:
            future.set_exception(CdpError(message["error"].get("message", "CDP error")))
        else:
            future.set_result(message.get("result", {}))
        return True

    def gone(self, session: str | None = None) -> None:
        for number, (owner, future) in list(self.pending.items()):
            if session is None or owner == session:
                self.pending.pop(number)
                if not future.done():
                    future.set_exception(CdpError("the target went away"))


async def answer(future: asyncio.Future, method: str, timeout: float) -> dict:
    try:
        return await asyncio.wait_for(future, timeout)
    except TimeoutError as error:
        raise CdpError(f"Chrome did not answer {method}") from error


def settle(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


class Outbox:
    def __init__(self, write: Callable[[str], object]) -> None:
        self.write = write
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    def put(self, text: str) -> None:
        self.queue.put_nowait(text)

    async def run(self) -> None:
        while True:
            await self.write(await self.queue.get())
