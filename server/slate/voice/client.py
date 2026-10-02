import asyncio
import time
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import aclosing

import modal

from slate.voice.audio import read_wav


async def send_audio(queue: modal.Queue, audio: AsyncIterable[bytes]) -> None:
    async for chunk in audio:
        await queue.put.aio(chunk)
    await queue.put.aio(None)


async def transcribe(pcm: bytes) -> AsyncIterator[str]:
    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(pcm), 3840):
            yield pcm[start : start + 3840]

    async with aclosing(transcribe_stream(chunks())) as stream:
        async for piece in stream:
            yield piece


async def transcribe_stream(
    audio: AsyncIterable[bytes], *, timings: dict | None = None
) -> AsyncIterator[str]:
    marks = {"requested": time.monotonic_ns()} if timings is not None else None
    model = modal.Cls.from_name("slate-stt", "SpeechToText")()
    async with modal.Queue.ephemeral() as incoming, modal.Queue.ephemeral() as outgoing:
        if marks is not None:
            marks["queues_ready"] = time.monotonic_ns()
        call = await model.transcribe.spawn.aio(incoming, outgoing, timings is not None)
        if marks is not None:
            marks["spawned"] = time.monotonic_ns()
        completed = False

        async def wait_for_result() -> None:
            nonlocal completed
            remote = await call.get.aio()
            if timings is not None:
                timings["remote"] = remote
            completed = True

        async def exchange() -> None:
            try:
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(send_audio(incoming, audio))
                    tasks.create_task(wait_for_result())
            finally:
                await outgoing.put.aio(None)

        transfer = asyncio.create_task(exchange())
        try:
            while (piece := await outgoing.get.aio()) is not None:
                if marks is not None:
                    marks.setdefault("first_text", time.monotonic_ns())
                yield piece
            await transfer
            if marks is not None:
                marks["completed"] = time.monotonic_ns()
                timings["client"] = marks
        finally:
            transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)
            if not completed:
                await call.cancel.aio()


async def speak(
    text: str, max_seconds: int = 15, *, timings: dict | None = None
) -> bytes:
    marks = {"requested": time.monotonic_ns()} if timings is not None else None
    model = modal.Cls.from_name("slate-tts", "TextToSpeech")()
    result = await model.speak.remote.aio(text, max_seconds, timings is not None)
    audio = result["audio"]
    read_wav(audio)
    if marks is not None:
        marks["completed"] = time.monotonic_ns()
        timings.update(client=marks, remote=result["timings"])
    return audio
