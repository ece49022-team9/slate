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
    .add_local_python_source("slate")
)

with image.imports():
    import numpy as np
    import torch
    from transformers import AutoProcessor, CsmForConditionalGeneration
    from transformers.models.mimi.modeling_mimi import (
        MimiConv1d,
        MimiConvTranspose1d,
        MimiResnetBlock,
    )

logger = logging.getLogger("slate.tts")
MODEL = "sesame/csm-1b"
REVISION = "c92a71e1c419772e25be7dc14d952c2521a740ab"


class AcousticFrameTiming:
    def __init__(self, profile: bool = True) -> None:
        self.profile = profile
        self.prompt_seen = False
        self.first_frame_ns = None
        self.ended_ns = None
        self.frames = 0
        self.gpu_start = torch.cuda.Event(enable_timing=True) if profile else None
        self.gpu_end = torch.cuda.Event(enable_timing=True) if profile else None

    def put(self, value) -> None:
        if not self.prompt_seen:
            self.prompt_seen = True
            return
        if self.profile and self.first_frame_ns is None:
            self.first_frame_ns = time.monotonic_ns()
        self.frames += 1

    def end(self) -> None:
        if self.profile:
            self.ended_ns = time.monotonic_ns()
            self.gpu_end.record()


class IncrementalMimiDecoder:
    def __init__(self, codec) -> None:
        config = codec.config
        if (
            not config.use_causal_conv
            or config.pad_mode != "constant"
            or config.trim_right_ratio != 1.0
            or config.sampling_rate != 24_000
            or config.audio_channels != 1
        ):
            raise ValueError("slate.tts: unsupported codec streaming configuration")
        self.codec = codec
        self.history = {}
        self.transformer_cache = None

    def layer(self, module, hidden):
        if isinstance(module, MimiConv1d):
            if not module.causal or module.conv.stride != (1,):
                raise ValueError("slate.tts: streaming requires causal stride-one conv")
            width = (module.conv.kernel_size[0] - 1) * module.conv.dilation[0]
            history = self.history.get(module)
            if history is None:
                history = hidden.new_zeros(*hidden.shape[:-1], width)
            joined = torch.cat((history, hidden), dim=-1)
            if width:
                self.history[module] = joined[..., -width:].clone()
            return module.conv(joined)
        if isinstance(module, MimiConvTranspose1d):
            if not module.causal or module.padding_left:
                raise ValueError("slate.tts: streaming requires right-trimmed conv")
            stride = module.conv.stride[0]
            width = (module.conv.kernel_size[0] - stride + stride - 1) // stride
            history = self.history.get(module)
            joined = hidden if history is None else torch.cat((history, hidden), dim=-1)
            self.history[module] = joined[..., -width:].clone() if width else None
            output = module(joined)
            return output[..., 0 if history is None else history.shape[-1] * stride :]
        if isinstance(module, MimiResnetBlock):
            residual = self.layer(module.shortcut, hidden)
            for layer in module.block:
                hidden = self.layer(layer, hidden)
            return residual + hidden
        return module(hidden)

    def decode(self, codes):
        hidden = self.codec.quantizer.decode(codes)
        if self.codec.upsample is not None:
            hidden = self.layer(self.codec.upsample, hidden)
        outputs = self.codec.decoder_transformer(
            hidden.transpose(1, 2),
            past_key_values=self.transformer_cache,
            use_cache=True,
            return_dict=True,
        )
        self.transformer_cache = outputs.past_key_values
        hidden = outputs.last_hidden_state.transpose(1, 2)
        for layer in self.codec.decoder.layers:
            hidden = self.layer(layer, hidden)
        return hidden


def encode_pcm(samples) -> bytes:
    if samples.size == 0 or not np.isfinite(samples).all():
        raise RuntimeError("slate.tts: model returned invalid audio")
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


class AcousticPcmStreamer(AcousticFrameTiming):
    def __init__(self, model, emit, profile: bool = False) -> None:
        super().__init__(profile)
        self.decoder = IncrementalMimiDecoder(model.codec_model)
        self.eos = model.config.codebook_eos_token_id
        self.device = model.device
        self.emit = emit
        self.samples = 0
        self.first_pcm_ns = None
        self.codec_decode_ms = 0.0
        self.pcm_encode_cpu_ms = 0.0
        self.queue_put_ms = 0.0

    def put(self, value) -> None:
        prompt = not self.prompt_seen
        super().put(value)
        if prompt or (value[0, :-1] == self.eos).all().item():
            return
        started = time.monotonic_ns() if self.profile else None
        codes = value.to(self.device).unsqueeze(-1)
        samples = self.decoder.decode(codes)[0, 0].float().cpu().numpy()
        encoded = time.monotonic_ns() if self.profile else None
        pcm = encode_pcm(samples)
        ready = time.monotonic_ns() if self.profile else None
        self.samples += samples.size
        if self.profile and self.first_pcm_ns is None:
            self.first_pcm_ns = ready
        self.emit(pcm)
        if self.profile:
            self.codec_decode_ms += (encoded - started) / 1_000_000
            self.pcm_encode_cpu_ms += (ready - encoded) / 1_000_000
            self.queue_put_ms += (time.monotonic_ns() - ready) / 1_000_000


def validate_request(text: str, max_seconds: int) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise ValueError("Text must contain 1–500 characters")
    if not 1 <= max_seconds <= 30:
        raise ValueError("max_seconds must be between 1 and 30")


def generate(model, inputs, max_seconds: int, observer, *, output_audio: bool):
    result = model.generate(
        **inputs,
        output_audio=output_audio,
        return_dict_in_generate=True,
        max_new_tokens=int(max_seconds * 12.5),
        **({"streamer": observer} if observer is not None else {}),
    )
    finished = (
        result.sequences[0, -1, :-1] == model.config.codebook_eos_token_id
    ).all()
    if not finished.item():
        raise RuntimeError("slate.tts: speech reached max_seconds before ending")
    return result


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
        self.model.codec_model.float()
        cache.commit()
        self.model_loaded_ns = time.monotonic_ns()
        self.model_load_ms = (self.model_loaded_ns - started) / 1_000_000
        logger.info("Model ready")

    @modal.method()
    def speak(self, text: str, max_seconds: int = 15, profile: bool = False) -> dict:
        validate_request(text, max_seconds)
        started = time.monotonic_ns()
        timing = (
            {
                "model_load_ms": self.model_load_ms,
                "model_age_ms": (started - self.model_loaded_ns) / 1_000_000,
                "function_call_id": modal.current_function_call_id(),
                "input_id": modal.current_input_id(),
                "gpu": torch.cuda.get_device_name(),
                "codec_dtype": "float32",
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
                result = generate(
                    self.model,
                    inputs,
                    max_seconds,
                    observer,
                    output_audio=True,
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
            samples = result.audio[0].detach().float().cpu().numpy().reshape(-1)
            if observer is not None:
                pcm_started = time.monotonic_ns()
                timing["audio_to_host_ms"] = (
                    pcm_started - postprocess_started
                ) / 1_000_000
            pcm = encode_pcm(samples)
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

    @modal.method()
    def speak_stream(
        self,
        text: str,
        outgoing: modal.Queue,
        max_seconds: int = 15,
        profile: bool = False,
    ) -> dict:
        validate_request(text, max_seconds)
        started = time.monotonic_ns() if profile else None
        try:
            inputs = self.processor(f"[0]{text.strip()}", add_special_tokens=True).to(
                "cuda"
            )
            observer = AcousticPcmStreamer(self.model, outgoing.put, profile)
            if profile:
                torch.cuda.synchronize()
                generate_started = time.monotonic_ns()
                observer.gpu_start.record()
            with torch.inference_mode():
                generate(self.model, inputs, max_seconds, observer, output_audio=False)
            if not observer.samples:
                raise RuntimeError("slate.tts: model returned no streamed audio")
            if not profile:
                return {}
            observer.gpu_end.synchronize()
            completed = time.monotonic_ns()
            return {
                "streamed": True,
                "model_load_ms": self.model_load_ms,
                "model_age_ms": (started - self.model_loaded_ns) / 1_000_000,
                "function_call_id": modal.current_function_call_id(),
                "input_id": modal.current_input_id(),
                "gpu": torch.cuda.get_device_name(),
                "codec_dtype": "float32",
                "preprocess_ms": (generate_started - started) / 1_000_000,
                "generate_ms": (completed - generate_started) / 1_000_000,
                "generate_kind": "acoustic_generation_codec_and_queue_span",
                "first_token_kind": "complete_acoustic_codebook_frame",
                "first_token_origin": "generate_start",
                "first_token_ms": (observer.first_frame_ns - generate_started)
                / 1_000_000,
                "first_pcm_ms": (observer.first_pcm_ns - generate_started) / 1_000_000,
                "codec_decode_ms": observer.codec_decode_ms,
                "codec_decode_kind": "incremental_codec_and_host_transfer",
                "pcm_encode_cpu_ms": observer.pcm_encode_cpu_ms,
                "queue_put_ms": observer.queue_put_ms,
                "audio_seconds": observer.samples / 24_000,
                "acoustic_frames_including_eos": observer.frames,
                "remote_total_ms": (completed - started) / 1_000_000,
            }
        except Exception:
            logger.exception("Streamed speech generation failed")
            raise
