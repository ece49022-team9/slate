import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

import modal

from slate.voice.audio import MAX_AUDIO_SECONDS, SAMPLE_BYTES, SAMPLE_RATE

app = modal.App("slate-stt")
cache = modal.Volume.from_name("slate-stt-models", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install("moshi==0.2.9", "torch==2.7.1", "huggingface-hub==0.33.5")
    .env({"HF_HOME": "/models", "NO_TORCH_COMPILE": "1"})
)

with image.imports():
    import numpy as np
    import torch
    from huggingface_hub import snapshot_download
    from moshi.models import LMGen, loaders

logger = logging.getLogger("slate.stt")
MODEL = "kyutai/stt-1b-en_fr"
REVISION = "1c34c6b4f7e9299bb61985f145052ff131005dde"


@app.cls(
    image=image,
    gpu=["L4", "A10", "L40S"],
    volumes={"/models": cache},
    max_containers=1,
    scaledown_window=60,
    timeout=300,
    startup_timeout=600,
)
class SpeechToText:
    @modal.enter()
    def load(self) -> None:
        started = time.monotonic_ns()
        logging.basicConfig(level=logging.INFO)
        logger.info("Loading %s at %s", MODEL, REVISION)
        directory = Path(snapshot_download(MODEL, revision=REVISION))
        config = json.loads((directory / "config.json").read_text())
        checkpoint = loaders.CheckpointInfo.from_hf_repo(
            MODEL,
            config_path=directory / "config.json",
            moshi_weights=directory / config.get("moshi_name", loaders.MOSHI_NAME),
            mimi_weights=directory / config["mimi_name"],
            tokenizer=directory / config["tokenizer_name"],
        )
        self.encoder = checkpoint.get_mimi(device="cuda")
        self.decoder = LMGen(checkpoint.get_moshi(device="cuda"), use_sampling=False)
        self.tokenizer = checkpoint.get_text_tokenizer()
        self.frame_samples = int(self.encoder.sample_rate / self.encoder.frame_rate)
        if self.encoder.sample_rate != SAMPLE_RATE:
            raise RuntimeError("slate.stt: model sample rate does not match the client")
        self.prefix_samples = round(
            checkpoint.stt_config.get("audio_silence_prefix_seconds", 0) * SAMPLE_RATE
        )
        self.tail_samples = round(
            (checkpoint.stt_config.get("audio_delay_seconds", 0) + 1) * SAMPLE_RATE
        )
        cache.commit()
        self.model_loaded_ns = time.monotonic_ns()
        self.model_load_ms = (self.model_loaded_ns - started) / 1_000_000
        logger.info("Model ready")

    def decode_frame(
        self, pcm: bytes, first: bool, timing: dict | None = None, *, tail: bool = False
    ) -> Iterator[str]:
        stage = "tail_decode_ms" if tail else "decode_ms"
        started = time.monotonic_ns() if timing is not None else 0
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        if timing is not None:
            begin = torch.cuda.Event(enable_timing=True)
            end = torch.cuda.Event(enable_timing=True)
            begin.record()
        frame = torch.from_numpy(samples).to("cuda").reshape(1, 1, -1)
        codes = self.encoder.encode(frame)
        if first:
            self.decoder.step(codes[:, :, :1])
        if timing is not None:
            end.record()
            end.synchronize()
            timing["cuda_spans"].append((stage, begin, end))
            elapsed = (time.monotonic_ns() - started) / 1_000_000
            timing[stage] += elapsed
            if tail and timing["first_text_emitted"]:
                timing["tail_decode_after_first_text_ms"] += elapsed
        for index in range(codes.shape[-1]):
            if timing is not None:
                started = time.monotonic_ns()
                begin = torch.cuda.Event(enable_timing=True)
                end = torch.cuda.Event(enable_timing=True)
                begin.record()
            tokens = self.decoder.step(codes[:, :, index : index + 1])
            piece = None
            if tokens is not None:
                token = tokens[0, 0, 0].item()
                if token not in (0, 3):
                    piece = self.tokenizer.id_to_piece(token).replace("▁", " ")
            if timing is not None:
                end.record()
                end.synchronize()
                timing["cuda_spans"].append((stage, begin, end))
                elapsed = (time.monotonic_ns() - started) / 1_000_000
                timing[stage] += elapsed
                if tail and timing["first_text_emitted"]:
                    timing["tail_decode_after_first_text_ms"] += elapsed
            if piece is not None:
                yield piece

    @modal.method()
    def transcribe(
        self, audio: modal.Queue, text: modal.Queue, profile: bool = False
    ) -> dict:
        started = time.monotonic_ns()
        timing = (
            {
                "decode_ms": 0.0,
                "tail_decode_ms": 0.0,
                "queue_get_ms": 0.0,
                "queue_put_ms": 0.0,
                "cuda_spans": [],
                "first_text_emitted": False,
                "tail_decode_after_first_text_ms": None,
                "model_load_ms": self.model_load_ms,
                "model_age_ms": (started - self.model_loaded_ns) / 1_000_000,
                "function_call_id": modal.current_function_call_id(),
                "input_id": modal.current_input_id(),
                "gpu": torch.cuda.get_device_name(),
                "decode_wall_kind": "synchronized_compute_excluding_queue_operations",
                "tail_decode_kind": "end_of_stream_flush_including_partial_frame",
            }
            if profile
            else None
        )
        for piece in self.decode(audio, timing):
            if timing is not None:
                timing.setdefault(
                    "first_text_ms", (time.monotonic_ns() - started) / 1_000_000
                )
                put_started = time.monotonic_ns()
            text.put(piece)
            if timing is not None:
                timing["queue_put_ms"] += (
                    time.monotonic_ns() - put_started
                ) / 1_000_000
                if not timing["first_text_emitted"]:
                    timing["first_text_emitted"] = True
                    timing["tail_decode_after_first_text_ms"] = 0.0
        if timing is None:
            return {}
        sync_started = time.monotonic_ns()
        timing.pop("first_text_emitted")
        spans = timing.pop("cuda_spans")
        if spans:
            spans[-1][2].synchronize()
        timing["gpu_drain_ms"] = (time.monotonic_ns() - sync_started) / 1_000_000
        timing["decode_gpu_ms"] = sum(
            begin.elapsed_time(end)
            for stage, begin, end in spans
            if stage == "decode_ms"
        )
        timing["tail_decode_gpu_ms"] = sum(
            begin.elapsed_time(end)
            for stage, begin, end in spans
            if stage == "tail_decode_ms"
        )
        timing["cuda_span_count"] = len(spans)
        timing["remote_total_ms"] = (time.monotonic_ns() - started) / 1_000_000
        return timing

    def decode(self, audio: modal.Queue, timing: dict | None = None) -> Iterator[str]:
        pending = bytearray(self.prefix_samples * SAMPLE_BYTES)
        frame_bytes = self.frame_samples * SAMPLE_BYTES
        received = 0
        first = True
        try:
            with (
                torch.inference_mode(),
                self.encoder.streaming(1),
                self.decoder.streaming(1),
            ):
                while True:
                    if timing is not None:
                        get_started = time.monotonic_ns()
                    chunk = audio.get(timeout=30)
                    if timing is not None:
                        timing["queue_get_ms"] += (
                            time.monotonic_ns() - get_started
                        ) / 1_000_000
                    if chunk is None:
                        if received == 0 or received % SAMPLE_BYTES:
                            raise ValueError(
                                "Audio must contain complete 16-bit samples"
                            )
                        pending.extend(bytes(self.tail_samples * SAMPLE_BYTES))
                        pending.extend(bytes(-len(pending) % frame_bytes))
                    else:
                        if not isinstance(chunk, bytes):
                            raise ValueError("Audio chunks must be PCM bytes")
                        received += len(chunk)
                        if received > SAMPLE_RATE * SAMPLE_BYTES * MAX_AUDIO_SECONDS:
                            raise ValueError("Audio exceeds the session length limit")
                        pending.extend(chunk)
                    while len(pending) >= frame_bytes:
                        frame = bytes(pending[:frame_bytes])
                        del pending[:frame_bytes]
                        yield from self.decode_frame(
                            frame, first, timing, tail=chunk is None
                        )
                        first = False
                    if chunk is None:
                        break
            logger.info(
                "Transcribed %.2f seconds", received / SAMPLE_BYTES / SAMPLE_RATE
            )
        except Exception:
            logger.exception("Transcription failed")
            raise
