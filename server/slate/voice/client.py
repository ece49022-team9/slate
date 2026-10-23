import asyncio
import time
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import aclosing

import modal

from slate.voice.audio import read_wav

QUEUE_BATCH = 256


async def drain(queue: modal.Queue) -> AsyncIterator:
    while True:
        for value in await queue.get_many.aio(QUEUE_BATCH):
            if value is None:
                return
            yield value


async def send_audio(queue: modal.Queue, audio: AsyncIterable[bytes]) -> None:
    pending: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def read() -> None:
        try:
            async for chunk in audio:
                pending.put_nowait(chunk)
        finally:
            pending.put_nowait(None)

    reader = asyncio.create_task(read())
    try:
        finished = False
        while not finished:
            chunks = [await pending.get()]
            while not pending.empty():
                chunks.append(pending.get_nowait())
            finished = chunks[-1] is None
            pcm = b"".join(chunk for chunk in chunks if chunk is not None)
            if pcm:
                await queue.put.aio(pcm)
        await reader
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
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
            async for piece in drain(outgoing):
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


async def speak_stream(
    text: str, max_seconds: int = 15, *, timings: dict | None = None
) -> AsyncIterator[bytes]:
    marks = {"requested": time.monotonic_ns()} if timings is not None else None
    model = modal.Cls.from_name("slate-tts", "TextToSpeech")()
    async with modal.Queue.ephemeral() as outgoing:
        if marks is not None:
            marks["queues_ready"] = time.monotonic_ns()
        call = await model.speak_stream.spawn.aio(
            text, outgoing, max_seconds, timings is not None
        )
        if marks is not None:
            marks["spawned"] = time.monotonic_ns()
        completed = False

        async def wait_for_result() -> None:
            nonlocal completed
            try:
                remote = await call.get.aio()
                completed = True
                if timings is not None:
                    timings["remote"] = remote
            finally:
                await outgoing.put.aio(None)

        transfer = asyncio.create_task(wait_for_result())
        try:
            async for pcm in drain(outgoing):
                if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
                    raise RuntimeError("slate.tts: invalid streamed PCM chunk")
                if marks is not None:
                    marks.setdefault("first_pcm", time.monotonic_ns())
                yield pcm
            await transfer
            if marks is not None:
                marks["completed"] = time.monotonic_ns()
                timings["client"] = marks
        finally:
            try:
                if not completed:
                    await call.cancel.aio()
            finally:
                transfer.cancel()
                await asyncio.gather(transfer, return_exceptions=True)
