import ast
import asyncio
import contextlib
import io
import json
import sys

RESULT = "__slate_result__"


class DeviceError(Exception):
    pass


class Channel:
    def __init__(self) -> None:
        self.pending: dict[int, asyncio.Future] = {}
        self.programs: asyncio.Queue[str | None] = asyncio.Queue()
        self.next_id = 0

    def send(self, message: dict) -> None:
        sys.__stdout__.write(json.dumps(message, allow_nan=False) + "\n")
        sys.__stdout__.flush()

    async def read(self, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            message = json.loads(line)
            if message["type"] == "execute":
                await self.programs.put(message["code"])
                continue
            future = self.pending.pop(message["id"])
            if message["type"] == "error":
                future.set_exception(DeviceError(message["error"]))
            else:
                future.set_result(message["value"])
        await self.programs.put(None)

    async def call(self, method: str, arguments: dict):
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        self.send(
            {"type": "call", "id": self.next_id, "method": method, "args": arguments}
        )
        return await future


class Device:
    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    async def set_orb(self, color: str, radius: float = 24) -> dict:
        return await self._channel.call("set_orb", {"color": color, "radius": radius})

    async def show_text(self, text: str) -> dict:
        return await self._channel.call("show_text", {"text": text})

    async def get_status(self) -> dict:
        return await self._channel.call("get_status", {})


async def run(code: str, namespace: dict):
    tree = ast.parse(code, "<agent>", "exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tree.body[-1] = ast.Assign(
            targets=[ast.Name(RESULT, ast.Store())], value=tree.body[-1].value
        )
        ast.fix_missing_locations(tree)
    namespace.pop(RESULT, None)
    program = compile(tree, "<agent>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    pending = eval(program, namespace)
    if pending is not None:
        await pending
    return namespace.pop(RESULT, None)


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1 << 20)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    channel = Channel()
    listener = asyncio.create_task(channel.read(reader))
    namespace = {"__name__": "__slate__", "device": Device(channel)}
    while (code := await channel.programs.get()) is not None:
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                value = await run(code, namespace)
            channel.send({"type": "done", "value": value, "output": printed.getvalue()})
        except Exception as error:
            channel.send(
                {
                    "type": "failed",
                    "error": {"type": type(error).__name__, "message": str(error)},
                    "output": printed.getvalue(),
                }
            )
    listener.cancel()


if __name__ == "__main__":
    asyncio.run(main())
