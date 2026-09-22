import json
import logging
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
    gpu="L4",
    volumes={"/models": cache},
    max_containers=1,
    scaledown_window=60,
    timeout=300,
    startup_timeout=600,
)
class SpeechToText:
    @modal.enter()
    def load(self) -> None:
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
        logger.info("Model ready")

    def decode_frame(self, pcm: bytes, first: bool) -> Iterator[str]:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        frame = torch.from_numpy(samples).to("cuda").reshape(1, 1, -1)
        codes = self.encoder.encode(frame)
        if first:
            self.decoder.step(codes[:, :, :1])
        for index in range(codes.shape[-1]):
            tokens = self.decoder.step(codes[:, :, index : index + 1])
            if tokens is not None:
                token = tokens[0, 0, 0].item()
                if token not in (0, 3):
                    yield self.tokenizer.id_to_piece(token).replace("▁", " ")

    @modal.method(is_generator=True)
    def transcribe(self, audio: modal.Queue) -> Iterator[str]:
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
                    chunk = audio.get(timeout=30)
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
                        yield from self.decode_frame(frame, first)
                        first = False
                    if chunk is None:
                        break
            logger.info(
                "Transcribed %.2f seconds", received / SAMPLE_BYTES / SAMPLE_RATE
            )
        except Exception:
            logger.exception("Transcription failed")
            raise
