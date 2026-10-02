import logging
import time

import modal

from slate.voice.audio import write_wav

app = modal.App("slate-tts")
cache = modal.Volume.from_name("slate-tts-models", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install(
        "transformers==4.57.6",
        "torch==2.7.1",
        "huggingface-hub==0.36.2",
        "librosa==0.11.0",
        "soundfile==0.13.1",
    )
    .env({"HF_HOME": "/models"})
)

with image.imports():
    import numpy as np
    import torch
    from transformers import AutoProcessor, CsmForConditionalGeneration

logger = logging.getLogger("slate.tts")
MODEL = "sesame/csm-1b"
REVISION = "c92a71e1c419772e25be7dc14d952c2521a740ab"


class AcousticFrameTiming:
    def __init__(self) -> None:
        self.prompt_seen = False
        self.first_frame_ns = None
        self.ended_ns = None
        self.frames = 0
        self.gpu_start = torch.cuda.Event(enable_timing=True)
        self.gpu_end = torch.cuda.Event(enable_timing=True)

    def put(self, value) -> None:
        if not self.prompt_seen:
            self.prompt_seen = True
            return
        if self.first_frame_ns is None:
            self.first_frame_ns = time.monotonic_ns()
        self.frames += 1

    def end(self) -> None:
        self.ended_ns = time.monotonic_ns()
        self.gpu_end.record()


@app.cls(
    image=image,
    gpu=["L4", "A10", "L40S"],
    volumes={"/models": cache},
    secrets=[modal.Secret.from_name("slate-huggingface", required_keys=["HF_TOKEN"])],
    max_containers=1,
    scaledown_window=60,
    timeout=300,
    startup_timeout=600,
)
class TextToSpeech:
    @modal.enter()
    def load(self) -> None:
        started = time.monotonic_ns()
        logging.basicConfig(level=logging.INFO)
        logger.info("Loading %s at %s", MODEL, REVISION)
        self.processor = AutoProcessor.from_pretrained(MODEL, revision=REVISION)
        self.model = (
            CsmForConditionalGeneration.from_pretrained(
                MODEL, revision=REVISION, dtype=torch.bfloat16
            )
            .to("cuda")
            .eval()
        )
        cache.commit()
        self.model_loaded_ns = time.monotonic_ns()
        self.model_load_ms = (self.model_loaded_ns - started) / 1_000_000
        logger.info("Model ready")

    @modal.method()
    def speak(self, text: str, max_seconds: int = 15, profile: bool = False) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError("Text must contain 1–500 characters")
        if not 1 <= max_seconds <= 30:
            raise ValueError("max_seconds must be between 1 and 30")
        started = time.monotonic_ns()
        timing = (
            {
                "model_load_ms": self.model_load_ms,
                "model_age_ms": (started - self.model_loaded_ns) / 1_000_000,
                "function_call_id": modal.current_function_call_id(),
                "input_id": modal.current_input_id(),
                "gpu": torch.cuda.get_device_name(),
                "first_token_kind": "complete_acoustic_codebook_frame",
                "first_token_origin": "generate_start",
            }
            if profile
            else {}
        )
        try:
            inputs = self.processor(f"[0]{text.strip()}", add_special_tokens=True).to(
                "cuda"
            )
            observer = AcousticFrameTiming() if profile else None
            if observer is not None:
                torch.cuda.synchronize()
                generate_started = time.monotonic_ns()
                timing["preprocess_ms"] = (generate_started - started) / 1_000_000
                observer.gpu_start.record()
            with torch.inference_mode():
                result = self.model.generate(
                    **inputs,
                    output_audio=True,
                    return_dict_in_generate=True,
                    max_new_tokens=int(max_seconds * 12.5),
                    do_sample=False,
                    depth_decoder_do_sample=False,
                    temperature=1.0,
                    depth_decoder_temperature=1.0,
                    **({"streamer": observer} if observer is not None else {}),
                )
            if observer is not None:
                decode_end = torch.cuda.Event(enable_timing=True)
                decode_end.record()
                decode_end.synchronize()
                generation_completed = time.monotonic_ns()
                timing["generate_ms"] = (
                    generation_completed - generate_started
                ) / 1_000_000
                timing["first_token_ms"] = (
                    (observer.first_frame_ns - generate_started) / 1_000_000
                    if observer.first_frame_ns is not None
                    else None
                )
                timing["acoustic_generation_ms"] = (
                    observer.ended_ns - generate_started
                ) / 1_000_000
                timing["remaining_acoustic_generation_ms"] = (
                    (observer.ended_ns - observer.first_frame_ns) / 1_000_000
                    if observer.first_frame_ns is not None
                    else None
                )
                timing["codec_decode_ms"] = (
                    generation_completed - observer.ended_ns
                ) / 1_000_000
                timing["codec_decode_kind"] = "post_generation_codec_and_bookkeeping"
                timing["acoustic_generation_gpu_ms"] = observer.gpu_start.elapsed_time(
                    observer.gpu_end
                )
                timing["codec_decode_gpu_ms"] = observer.gpu_end.elapsed_time(
                    decode_end
                )
                timing["acoustic_frames_including_eos"] = observer.frames
                postprocess_started = time.monotonic_ns()
            finished = (
                result.sequences[0, -1, :-1] == self.model.config.codebook_eos_token_id
            ).all()
            if not finished.item():
                raise RuntimeError(
                    "slate.tts: speech reached max_seconds before ending"
                )
            samples = result.audio[0].detach().float().cpu().numpy().reshape(-1)
            if observer is not None:
                pcm_started = time.monotonic_ns()
                timing["audio_to_host_ms"] = (
                    pcm_started - postprocess_started
                ) / 1_000_000
            if samples.size == 0 or not np.isfinite(samples).all():
                raise RuntimeError("slate.tts: model returned invalid audio")
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            if observer is not None:
                wav_started = time.monotonic_ns()
                timing["pcm_encode_cpu_ms"] = (wav_started - pcm_started) / 1_000_000
            logger.info("Generated %.2f seconds", samples.size / 24_000)
            audio = write_wav(pcm)
            if observer is not None:
                completed = time.monotonic_ns()
                timing["wav_encode_cpu_ms"] = (completed - wav_started) / 1_000_000
                timing["postprocess_ms"] = (completed - postprocess_started) / 1_000_000
                timing["audio_seconds"] = samples.size / 24_000
                timing["remote_total_ms"] = (completed - started) / 1_000_000
            return {"audio": audio, "timings": timing}
        except Exception:
            logger.exception("Speech generation failed")
            raise
