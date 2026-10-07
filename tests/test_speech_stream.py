import asyncio
import hashlib
import time
import unittest
from contextlib import aclosing, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import modal
from slate.voice.client import speak_stream
from slate.voice.tts import (
    MODEL,
    REVISION,
    AcousticPcmStreamer,
    IncrementalMimiDecoder,
    TextToSpeech,
    app,
    cache,
    encode_pcm,
    generate,
    image,
)

with image.imports():
    import numpy as np
    import torch
    from transformers import (
        AutoProcessor,
        CsmForConditionalGeneration,
        MimiConfig,
        MimiModel,
    )
    from transformers.models.mimi.modeling_mimi import MimiEuclideanCodebook


class SpeechStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.complete = asyncio.Event()
        self.cancel_finished = asyncio.Event()

        async def get_result():
            await self.complete.wait()
            return {"streamed": True}

        async def cancel():
            await asyncio.sleep(0)
            self.cancel_finished.set()

        self.call = SimpleNamespace(
            get=SimpleNamespace(aio=AsyncMock(side_effect=get_result)),
            cancel=SimpleNamespace(aio=AsyncMock(side_effect=cancel)),
        )
        self.remote_queue = SimpleNamespace(
            put=SimpleNamespace(aio=self.queue.put),
            get=SimpleNamespace(aio=self.queue.get),
        )
        self.spawn = AsyncMock(return_value=self.call)
        self.model = SimpleNamespace(
            speak_stream=SimpleNamespace(spawn=SimpleNamespace(aio=self.spawn))
        )

        @asynccontextmanager
        async def ephemeral():
            try:
                yield self.remote_queue
            finally:
                self.closed.set()

        self.patch_queue = patch("slate.voice.client.modal.Queue.ephemeral", ephemeral)
        self.patch_model = patch(
            "slate.voice.client.modal.Cls.from_name",
            return_value=Mock(return_value=self.model),
        )
        self.patch_queue.start()
        self.patch_model.start()
        self.addCleanup(self.patch_queue.stop)
        self.addCleanup(self.patch_model.stop)

    async def test_pcm_arrives_before_producer_completion_and_close_awaits_cancel(self):
        async with aclosing(speak_stream("hello")) as stream:
            first = asyncio.create_task(anext(stream))
            await self.queue.put(b"\x01\x00" * 1920)
            self.assertEqual(len(await first), 3840)
            self.assertFalse(self.complete.is_set())
            self.assertFalse(self.closed.is_set())
        self.assertTrue(self.cancel_finished.is_set())
        self.assertTrue(self.closed.is_set())
        self.call.cancel.aio.assert_awaited_once()

    async def test_complete_pcm_stream_preserves_chunks_and_optional_timings(self):
        chunks = [b"\x01\x00" * 1920, b"\x02\x00" * 1920]
        for chunk in chunks:
            await self.queue.put(chunk)
        self.complete.set()
        timings = {}
        received = [chunk async for chunk in speak_stream("hello", timings=timings)]
        self.assertEqual(received, chunks)
        self.assertEqual(timings["remote"], {"streamed": True})
        self.assertLessEqual(
            timings["client"]["first_pcm"], timings["client"]["completed"]
        )
        self.spawn.assert_awaited_once_with("hello", self.remote_queue, 15, True)
        self.call.cancel.aio.assert_not_awaited()

    async def test_remote_failure_propagates_after_already_emitted_audio(self):
        await self.queue.put(b"\x01\x00")
        self.call.get.aio.side_effect = RuntimeError("EOS limit reached")
        with self.assertRaisesRegex(RuntimeError, "EOS limit"):
            async with aclosing(speak_stream("hello")) as stream:
                self.assertEqual(await anext(stream), b"\x01\x00")
                await anext(stream)
        self.assertTrue(self.closed.is_set())

    async def test_cancel_while_waiting_for_first_audio_cancels_remote(self):
        first = asyncio.create_task(anext(speak_stream("hello")))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertTrue(self.cancel_finished.is_set())
        self.assertTrue(self.closed.is_set())

    async def test_invalid_pcm_cancels_remote_and_closes_queue(self):
        await self.queue.put(b"\x01")
        with self.assertRaisesRegex(RuntimeError, "invalid streamed PCM"):
            await anext(speak_stream("hello"))
        self.assertTrue(self.cancel_finished.is_set())
        self.assertTrue(self.closed.is_set())


@app.function(
    image=image.add_local_python_source("slate"),
    gpu="A10",
    volumes={"/models": cache},
    secrets=[modal.Secret.from_name("slate-huggingface", required_keys=["HF_TOKEN"])],
    timeout=300,
)
def validate_codec_stream(outgoing: modal.Queue, generate_speech: bool = False) -> dict:
    torch.set_num_threads(1)
    torch.manual_seed(17)
    tiny = MimiModel(
        MimiConfig(
            hidden_size=32,
            num_filters=4,
            upsampling_ratios=[2, 2],
            codebook_dim=32,
            codebook_size=32,
            vector_quantization_hidden_dimension=32,
            num_quantizers=4,
            num_hidden_layers=2,
            intermediate_size=64,
            num_attention_heads=4,
            num_key_value_heads=4,
            upsample_groups=32,
            frame_rate=3000,
        )
    ).eval()
    for module in tiny.modules():
        if isinstance(module, MimiEuclideanCodebook):
            module.embed_sum.normal_()
    codes = torch.randint(0, 32, (1, 4, 23))
    with torch.inference_mode():
        expected = tiny.decode(codes).audio_values
        for sizes in ([1] * 23, [4, 5, 7, 7]):
            decoder = IncrementalMimiDecoder(tiny)
            offset = 0
            chunks = []
            for size in sizes:
                chunks.append(decoder.decode(codes[..., offset : offset + size]))
                offset += size
            torch.testing.assert_close(
                torch.cat(chunks, dim=-1), expected, atol=1e-5, rtol=1e-5
            )
    processor = AutoProcessor.from_pretrained(MODEL, revision=REVISION)
    model = (
        CsmForConditionalGeneration.from_pretrained(
            MODEL, revision=REVISION, dtype=torch.bfloat16
        )
        .to("cuda")
        .eval()
    )
    model.codec_model.float()
    pcm_chunks = []
    first_pcm_ns = None

    def emit(pcm):
        nonlocal first_pcm_ns
        if first_pcm_ns is None:
            first_pcm_ns = time.monotonic_ns()
        pcm_chunks.append(pcm)
        outgoing.put(pcm)

    observer = AcousticPcmStreamer(model, emit)
    with torch.inference_mode():
        started = time.monotonic_ns()
        if generate_speech:
            inputs = processor(
                "[0]Seven plus five is twelve.", add_special_tokens=True
            ).to("cuda")
            result = generate(model, inputs, 15, observer, output_audio=False)
            frames = result.sequences[0, :-1]
        else:
            frames = torch.randint(
                0,
                model.codec_model.config.codebook_size,
                (23, model.codec_model.config.num_quantizers),
            )
            observer.put(torch.zeros((1, 1), dtype=torch.long))
            for frame in frames:
                observer.put(frame.unsqueeze(0))
            observer.end()
        generation_completed = time.monotonic_ns()
        baseline = (
            model.codec_model.decode(frames.transpose(0, 1).unsqueeze(0).to("cuda"))
            .audio_values[0, 0]
            .float()
            .cpu()
            .numpy()
        )
    streamed = (
        np.frombuffer(b"".join(pcm_chunks), dtype="<i2").astype(np.float32) / 32767
    )
    reference = (
        np.frombuffer(encode_pcm(baseline), dtype="<i2").astype(np.float32) / 32767
    )
    assert streamed.shape == reference.shape, (streamed.shape, reference.shape)
    relative_rmse = float(
        np.sqrt(np.mean((streamed - reference) ** 2))
        / max(float(np.sqrt(np.mean(reference**2))), 1e-8)
    )
    assert relative_rmse < 0.01, relative_rmse
    assert len(pcm_chunks) > 1
    assert first_pcm_ns < generation_completed
    return {
        "chunks": len(pcm_chunks),
        "samples": len(streamed),
        "relative_rmse": relative_rmse,
        "tiny_partition_parity": True,
        "input_kind": "generated_speech" if generate_speech else "seeded_codebooks",
        "pcm_sha256": hashlib.sha256(b"".join(pcm_chunks)).hexdigest(),
        "first_pcm_ms": (first_pcm_ns - started) / 1_000_000,
        "generation_completed_ms": (generation_completed - started) / 1_000_000,
        "gpu": torch.cuda.get_device_name(),
    }


@app.local_entrypoint()
async def main(cancel: bool = False, generate_speech: bool = False):
    async with modal.Queue.ephemeral() as outgoing:
        if cancel:
            call = await TextToSpeech().speak_stream.spawn.aio(
                "Seven plus five is twelve.", outgoing, 15, False
            )
            cancelled = False
            try:
                chunk = await outgoing.get.aio(timeout=180)
                assert isinstance(chunk, bytes) and len(chunk) == 3840
                await call.cancel.aio()
                cancelled = True
                async with asyncio.timeout(30):
                    while True:
                        graph = await call.get_call_graph.aio()
                        if graph and graph[0].status.name == "TERMINATED":
                            print(
                                {
                                    "first_chunk_bytes": len(chunk),
                                    "status": "TERMINATED",
                                    "function_call_id": call.object_id,
                                }
                            )
                            break
                        await asyncio.sleep(0.1)
            finally:
                if not cancelled:
                    await call.cancel.aio()
            return
        call = await validate_codec_stream.spawn.aio(outgoing, generate_speech)

        async def wait():
            try:
                return await call.get.aio()
            finally:
                await outgoing.put.aio(None)

        transfer = asyncio.create_task(wait())
        chunks = []
        try:
            while (chunk := await outgoing.get.aio()) is not None:
                chunks.append(chunk)
            result = await transfer
            assert result["pcm_sha256"] == hashlib.sha256(b"".join(chunks)).hexdigest()
            print(result)
        finally:
            if not transfer.done():
                await call.cancel.aio()
            transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
