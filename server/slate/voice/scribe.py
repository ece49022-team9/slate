import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import AsyncIterable, AsyncIterator
from urllib.parse import urlencode

import websockets

from slate.voice.audio import SAMPLE_RATE

logger = logging.getLogger("slate.voice.scribe")
URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
MODEL = "scribe_v2_realtime"
KEYTERMS = ("Slate",)
START_SECONDS = 5
FINAL_WAIT_SECONDS = 1.5


def chunk(pcm: bytes, commit: bool = False) -> str:
    return json.dumps(
        {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm).decode(),
            "commit": commit,
            "sample_rate": SAMPLE_RATE,
        }
    )


async def transcribe_stream(
    audio: AsyncIterable[bytes], *, timings: dict | None = None
) -> AsyncIterator[str]:
    marks = {"requested": time.monotonic_ns()}
    query = urlencode(
        [
            ("model_id", MODEL),
            ("audio_format", f"pcm_{SAMPLE_RATE}"),
            ("commit_strategy", "manual"),
            *(("keyterms", term) for term in KEYTERMS),
        ]
    )
    async with asyncio.timeout(START_SECONDS):
        socket = await websockets.connect(
            f"{URL}?{query}",
            additional_headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
            max_size=None,
        )
        opening = json.loads(await socket.recv())
    if opening.get("message_type") != "session_started":
        await socket.close()
        logger.error("Scribe refused to start: %s", opening)
        raise RuntimeError(f"slate.voice.scribe: {opening.get('message_type')}")
    marks["started"] = time.monotonic_ns()
    committed: list[str] = []
    partial = ""
    final = asyncio.Event()

    async def send() -> None:
        async for pcm in audio:
            await socket.send(chunk(pcm))
        marks["ended"] = time.monotonic_ns()
        await socket.send(chunk(b"", commit=True))

    async def receive() -> None:
        nonlocal partial
        async for message in socket:
            event = json.loads(message)
            kind = event.get("message_type")
            if "error" in event:
                logger.error("Scribe ended a transcription: %s", event)
                raise RuntimeError(f"slate.voice.scribe: {kind}: {event['error']}")
            if kind == "partial_transcript":
                partial = event.get("text", "").strip()
            elif kind == "committed_transcript":
                if text := event.get("text", "").strip():
                    committed.append(text)
                partial = ""
                if "ended" in marks:
                    final.set()

    async with socket:
        receiver = asyncio.create_task(receive())
        try:
            await send()
            waiter = asyncio.create_task(final.wait())
            await asyncio.wait(
                {waiter, receiver},
                timeout=FINAL_WAIT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            waiter.cancel()
            if receiver.done() and receiver.exception():
                raise receiver.exception()
            if not final.is_set():
                logger.warning(
                    "Scribe sent no final within %ss; using what it heard",
                    FINAL_WAIT_SECONDS,
                )
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    marks["final"] = time.monotonic_ns()
    if timings is not None:
        timings["client"] = marks
    text = " ".join([*committed, partial]).strip()
    if text:
        yield text
