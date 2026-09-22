# Slate

A portable voice assistant for everyday tasks. The ESP32-S3 handles audio and device feedback. Python handles speech, model calls, and browser tasks. React handles setup and approvals.

## Code

| Path | Work |
| --- | --- |
| `firmware/` | ESP32-S3 firmware |
| `server/slate/voice/` | Audio streaming, speech recognition and generation |
| `server/slate/agent/` | Model calls and task execution |
| `server/slate/browser/` | Browser sessions, tools and result checks |
| `server/slate/access/` | Accounts, pairing, permissions and credentials |
| `web/` | Setup, approvals and user control |
| `hardware/` | KiCad project and board sources |

One Python backend for now. Keep model calls with the agent and permission checks in access. The web app never gets provider secrets.

## Local setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Node 24](https://nodejs.org/), then:

```sh
make setup
make server
```

In another terminal:

```sh
make web
```

Open [localhost:5173](http://127.0.0.1:5173). [API docs](http://127.0.0.1:8000/api/docs) and [OpenAPI](http://127.0.0.1:8000/api/openapi.json) come from the backend.

`make check` runs lint, formatting, audio tests and the web build. CI also checks that the backend starts and responds.

## Speech

[Kyutai STT](https://modal.com/docs/examples/streaming_kyutai_stt) and [Sesame CSM 1B](https://huggingface.co/docs/transformers/model_doc/csm) run in separate Modal apps, `slate-stt` and `slate-tts`. Both cache their weights and scale to zero when idle.

Use the `sudarshan-1` Modal profile. CSM needs a Modal secret named `slate-huggingface` containing `HF_TOKEN`, with access to `sesame/csm-1b`.

```sh
make voice-deploy
make voice-check
```

The check generates speech, saves `.local/voice-check.wav`, and transcribes it. To use each model:

```sh
export MODAL_PROFILE=sudarshan-1
uv run --group voice python -m slate.voice tts "Slate is ready."
uv run --group voice python -m slate.voice stt .local/speech.wav
```

STT accepts mono 24 kHz, 16-bit PCM WAV files up to 120 seconds. TTS returns the same format, with a 15-second default limit (`--max-seconds`, up to 30). CSM has no reference voice yet. Calls use Modal authentication; the ESP32 audio connection is still separate.

## Firmware

The target is ESP32-S3. Install ESP-IDF 5.5.5 and Espressif's QEMU, then build and boot it headlessly:

```sh
make firmware-setup
make firmware-check
```

`make firmware` builds. `make firmware-sim` leaves the serial console open. Exit with Ctrl-A, then X. The setup uses `~/esp/esp-idf-v5.5.5` and `~/.espressif`, with Python packages managed by uv.

QEMU checks firmware startup. It does not test the board's microphone, speaker, display, or Wi-Fi. [LiveKit's ESP32 examples](https://github.com/livekit/client-sdk-esp32/tree/main/components/livekit/examples) cover the later audio connection. PlatformIO still needs a `platformio.ini`.

[Proposal](https://docs.google.com/document/d/1dz02PJORUFB1m--cltmt9tKI_VPXVcO9dAPNODxkFbo/edit) · [Work split](https://notes.granola.ai/t/a576ba7f-ef74-42ac-b631-dfefc260f884-008umkv4)
