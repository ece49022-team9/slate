import asyncio
from collections.abc import AsyncIterator

import modal

from slate.voice.audio import read_wav


async def send_audio(queue: modal.Queue, pcm: bytes) -> None:
    for start in range(0, len(pcm), 3840):
        await queue.put.aio(pcm[start : start + 3840])
    await queue.put.aio(None)


async def transcribe(pcm: bytes) -> AsyncIterator[str]:
    model = modal.Cls.from_name("slate-stt", "SpeechToText")()
    async with modal.Queue.ephemeral() as queue:
        sender = asyncio.create_task(send_audio(queue, pcm))
        try:
            async for piece in model.transcribe.remote_gen.aio(queue):
                yield piece
            await sender
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)


async def speak(text: str, max_seconds: int = 15) -> bytes:
    model = modal.Cls.from_name("slate-tts", "TextToSpeech")()
    audio = await model.speak.remote.aio(text, max_seconds)
    read_wav(audio)
    return audio
