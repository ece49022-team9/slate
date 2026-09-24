import asyncio
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


async def transcribe_stream(audio: AsyncIterable[bytes]) -> AsyncIterator[str]:
    model = modal.Cls.from_name("slate-stt", "SpeechToText")()
    async with modal.Queue.ephemeral() as incoming, modal.Queue.ephemeral() as outgoing:
        call = await model.transcribe.spawn.aio(incoming, outgoing)
        completed = False

        async def wait_for_result() -> None:
            nonlocal completed
            await call.get.aio()
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
                yield piece
            await transfer
        finally:
            transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)
            if not completed:
                await call.cancel.aio()


async def speak(text: str, max_seconds: int = 15) -> bytes:
    model = modal.Cls.from_name("slate-tts", "TextToSpeech")()
    audio = await model.speak.remote.aio(text, max_seconds)
    read_wav(audio)
    return audio
