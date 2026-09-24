import logging

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


@app.cls(
    image=image,
    gpu="L4",
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
        logger.info("Model ready")

    @modal.method()
    def speak(self, text: str, max_seconds: int = 15) -> bytes:
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError("Text must contain 1–500 characters")
        if not 1 <= max_seconds <= 30:
            raise ValueError("max_seconds must be between 1 and 30")
        try:
            inputs = self.processor(f"[0]{text.strip()}", add_special_tokens=True).to(
                "cuda"
            )
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
                )
            finished = (
                result.sequences[0, -1, :-1] == self.model.config.codebook_eos_token_id
            ).all()
            if not finished.item():
                raise RuntimeError(
                    "slate.tts: speech reached max_seconds before ending"
                )
            samples = result.audio[0].detach().float().cpu().numpy().reshape(-1)
            if samples.size == 0 or not np.isfinite(samples).all():
                raise RuntimeError("slate.tts: model returned invalid audio")
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            logger.info("Generated %.2f seconds", samples.size / 24_000)
            return write_wav(pcm)
        except Exception:
            logger.exception("Speech generation failed")
            raise
