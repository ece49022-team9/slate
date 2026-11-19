import argparse
import asyncio
import json
import logging
import os
import tempfile
from unittest.mock import patch

import httpx
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

PORT = 8000
OWNER = "slate-local-demo"
BASE = f"http://127.0.0.1:{PORT}"
heard = asyncio.Queue()


class DemoAgent:
    def __init__(self) -> None:
        self.pending = set()
        self.timings = {}
        self.last_run = {}
        self.decided = asyncio.Event()
        self.choice = None
        self.runs = 0

    async def run(self, message, progress, *, device_context):
        self.runs += 1
        run_id = f"run_{self.runs}"
        await progress({"type": "tool.started", "tool": "web_search"})
        await progress({"type": "tool.completed", "tool": "web_search"})
        if "pay" not in message.lower() and "buy" not in message.lower():
            answer = "Done. Nothing here needed your approval."
            await progress({"type": "answer.complete", "text": answer})
            return answer
        self.decided.clear()
        self.choice = None
        await progress({"type": "tool.started", "tool": "browser_click"})
        await progress(
            {
                "type": "approval.request",
                "run_id": run_id,
                "request_id": "req_1",
                "command": 'Click "Pay $84.12" on duke-energy.com',
            }
        )
        await self.decided.wait()
        await progress({"type": "tool.completed", "tool": "browser_click"})
        answer = "Paid $84.12." if self.choice == "once" else "I did not pay."
        await progress({"type": "answer.complete", "text": answer})
        return answer

    async def approve(self, run_id, request_id, choice):
        self.choice = choice
        self.decided.set()

    async def close(self):
        pass


async def transcribe(chunks, timings=None):
    async for _ in chunks:
        pass
    yield await heard.get()


async def speak(text, *, timings):
    yield b"\x00\x00" * 480


def show(event: dict) -> None:
    kind = event["type"]
    if kind == "approval":
        print(f"  device  <- approval needed: {event['command']}")
        print("             answer on the dashboard, or type y / n here")
    elif kind == "approval_resolved":
        print(f"  device  <- approval {event['status']} by {event['decided_by']}")
    elif kind == "tool":
        print(f"  device  <- agent is using {event['tool']}")
    elif kind == "reply" and event.get("final"):
        print(f"  device  <- reply: {event['text']}")
    elif kind == "cancelled":
        print("  device  <- turn cancelled")


async def reconnect(token: str) -> None:
    url = f"ws://127.0.0.1:{PORT}/api/device/socket"
    try:
        async with connect(
            url, additional_headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            await ws.recv()
    except InvalidStatus as refused:
        code = refused.response.status_code
        print(f"  device  -> reconnect refused (HTTP {code}): the token was revoked")
    except ConnectionClosed as closed:
        print(f"  device  -> reconnect refused (code {closed.rcvd.code})")
    print("\nPress Enter to stop the demo.")


async def device(token: str, said: list[str]) -> None:
    url = f"ws://127.0.0.1:{PORT}/api/device/socket"
    async with connect(
        url, additional_headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        await ws.send(json.dumps({"type": "hello"}))
        print("  device  -> connected with its own token\n")
        approval: dict = {}
        turn = asyncio.Event()
        closed = asyncio.Event()

        async def ask(prompt: str) -> str | None:
            reading = asyncio.ensure_future(asyncio.to_thread(input, prompt))
            stopping = asyncio.ensure_future(closed.wait())
            done, _ = await asyncio.wait(
                {reading, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
            stopping.cancel()
            if reading not in done:
                return None
            try:
                return reading.result()
            except EOFError:
                return None

        async def listen() -> None:
            try:
                async for frame in ws:
                    if isinstance(frame, bytes):
                        continue
                    receive(json.loads(frame))
            except ConnectionClosed:
                pass
            print(f"\n  device  <- disconnected by the server (code {ws.close_code})")
            closed.set()

        def receive(event: dict) -> None:
            show(event)
            if event["type"] == "approval":
                approval.update(event)
            if event["type"] in ("approval_resolved", "cancelled"):
                approval.clear()
            if (event["type"] == "reply" and event.get("final")) or event["type"] in (
                "cancelled",
                "error",
            ):
                turn.set()

        listener = asyncio.create_task(listen())
        try:
            while True:
                if said:
                    text = said.pop(0)
                    print(f"you say: {text}")
                else:
                    text = await ask("say something to Slate (empty to quit): ")
                if not text or not text.strip() or closed.is_set():
                    return
                turn.clear()
                await heard.put(text.strip())
                await ws.send(json.dumps({"type": "start"}))
                await ws.send(b"\x10\x00" * 1600)
                await ws.send(json.dumps({"type": "end"}))
                while not turn.is_set():
                    if closed.is_set():
                        return
                    if approval and not said:
                        answer = await ask("")
                        if answer is None:
                            return
                        if approval and answer.strip().lower() in ("y", "n"):
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "approve",
                                        "turn_id": approval["turn_id"],
                                        "run_id": approval["run_id"],
                                        "request_id": approval["request_id"],
                                        "choice": "once"
                                        if answer.strip().lower() == "y"
                                        else "deny",
                                    }
                                )
                            )
                        continue
                    await asyncio.sleep(0.1)
                print()
        except ConnectionClosed:
            await closed.wait()
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)


async def api_device(device_id: str) -> dict:
    async with httpx.AsyncClient(
        base_url=BASE, headers={"Authorization": f"Bearer {OWNER}"}
    ) as api:
        listed = (await api.get("/api/devices")).json()
    return next(item for item in listed if item["id"] == device_id)


async def main(said: list[str]) -> None:
    from slate.app import app

    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.ERROR)

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    async with httpx.AsyncClient(
        base_url=BASE, headers={"Authorization": f"Bearer {OWNER}"}
    ) as api:
        paired = (await api.post("/api/devices", json={"name": "Slate-Demo"})).json()
    print("Slate backend is running on this Mac.\n")
    print("Open the dashboard in a second terminal:")
    print(
        f"  VITE_SLATE_URL=ws://127.0.0.1:{PORT} VITE_SLATE_DEVICE_TOKEN={OWNER} "
        "npm --prefix web run dev"
    )
    print("  then open http://127.0.0.1:5173\n")
    print(f"paired {paired['device']['id']} (Slate-Demo)")
    print('Try: "Pay my electric bill", then approve or deny on the dashboard.\n')
    try:
        await device(paired["token"], said)
        if (await api_device(paired["device"]["id"]))["revoked_at"]:
            await reconnect(paired["token"])
    finally:
        server.should_exit = True
        await serving


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--say", action="append", default=[])
    args = parser.parse_args()
    with (
        tempfile.TemporaryDirectory() as home,
        patch.dict(os.environ, {"SLATE_DEVICE_TOKEN": OWNER, "SLATE_HOME": home}),
        patch("slate.voice.session.create_agent", return_value=DemoAgent()),
        patch("slate.voice.session.transcribe_stream", transcribe),
        patch("slate.voice.session.speak_stream", speak),
    ):
        asyncio.run(main(args.say))
