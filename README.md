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

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), [Node 24](https://nodejs.org/), and [LiveKit](https://docs.livekit.io/transport/self-hosting/local/) (`brew install livekit` on macOS), then:

```sh
make setup
```

Run `make livekit`, `make server`, and `make web` in separate terminals.

Open [localhost:5173](http://127.0.0.1:5173), connect your microphone, and hold Talk. Release it to finish; Cancel discards the recording. This test shows transcripts only.

`make check` runs lint, formatting, tests and the web build. [API docs](http://127.0.0.1:8000/api/docs) come from the backend.

## Microphone connection

The browser sends a LiveKit microphone track. The backend converts it to mono 24 kHz PCM, streams it to Kyutai, and sends text back. To use a recording as the device:

```sh
uv run python -m slate.voice simulate .local/voice-check.wav
```

The simulator reads a mono 24 kHz, 16-bit WAV and publishes 16 kHz audio by default. `--sample-rate 48000` tests a browser-rate stream. It uses the same room, audio track, and controls as the browser.

The firmware will use the same contract:

| Operation | Interface |
| --- | --- |
| Connect | `POST /api/voice/sessions` with `{}` returns a room URL, participant token, and worker identity |
| Send audio | Publish one LiveKit microphone track |
| Start | Call the worker's `start_turn` RPC; keep the returned turn ID |
| Finish | Stop sending audio, then call `end_turn` with that ID |
| Cancel | Call `cancel_turn` with that ID |
| Read text | Receive `slate.transcript` packets: `{turn_id, text, final}` or `{turn_id, error}` |

Development allows one connected device at a time. Sessions last ten minutes; recordings are limited to two minutes. The token endpoint is local-only until device pairing is built. For another LiveKit server, set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` together on the backend.

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
uv run python -m slate.voice tts "Slate is ready."
uv run python -m slate.voice stt .local/speech.wav
```

STT accepts mono 24 kHz, 16-bit PCM WAV files up to 120 seconds. TTS returns the same format and errors if it cannot finish within `--max-seconds` (default 15, max 30). CSM has no reference voice yet. Modal credentials stay on the backend.

## Firmware

The target is ESP32-S3. Install ESP-IDF 5.5.5 and Espressif's QEMU, then build and boot it headlessly:

```sh
make firmware-setup
make firmware-check
```

`make firmware` builds. `make firmware-sim` leaves the serial console open. Exit with Ctrl-A, then X. The setup uses `~/esp/esp-idf-v5.5.5` and `~/.espressif`, with Python packages managed by uv.

QEMU runs the same FreeRTOS state controller, channel selection, DC filter, and microphone buffer as the board build. `make firmware-check` checks capture gating, left/right/mix selection, overflow rejection, and buffer reset. `make firmware-mic-check` uses Hypothesis to generate 60 stereo audio cases, replay them with different chunk boundaries, and compare the firmware output against an independent PCM calculation. Failures are shrunk and saved for replay.

The microphone schematic contains two MP34DT01-M parts: MK1 has L/R grounded and MK2 has L/R tied high. They share PDM clock/data. The S3 driver uses I2S0 to convert both PDM slots to interleaved 16 kHz PCM. The shared pipeline selects left, right, or an equal-weight mono mix, removes DC, and queues up to one second of audio. Overflow rejects a whole frame; mute/cancel clears queued samples and filter history, while finishing a turn preserves buffered audio for draining. Gain is unity; the old fixed 4x amplification is removed to avoid clipping recorded speech.

To send simulated microphone audio through the firmware and the real transcription service, start `make livekit` and `make server`, then run:

```sh
MODAL_PROFILE=sudarshan-1 uv run python -m slate.voice firmware .local/voice-check.wav
```

Use a 16-bit WAV at 16, 24, or 48 kHz, up to 119 seconds. Stereo WAVs represent the two microphone slots; mono WAVs populate only the left slot. `--channel left` is the default; `--channel right` selects the other mic and `--channel mix` averages both. For a mono WAV, mix therefore halves the signal level and right produces silence.

The command builds and boots QEMU, injects stereo PCM at the boundary after hardware PDM conversion, reads processed mono audio from the firmware buffer through UART1, and publishes it as a LiveKit microphone track. The existing start/end-turn RPCs and transcription service handle the recording. A host bridge runs WebRTC; this does not implement on-device LiveKit networking or emulate PDM clock edges, DMA, or physical microphones. QEMU has no I2S/PDM emulation.

`make firmware-hardware` compiles the S3 PDM adapter and peripheral drivers with pinned PlatformIO dependencies. Physical capture remains disabled without confirmed S3 GPIO assignments. Defining both `SLATE_PDM_CLK` and `SLATE_PDM_DATA` enables the adapter for board bring-up; a physical transport consumer still needs to drain the audio buffer. The older display/haptic pin assignments must also be replaced before those peripherals are enabled. [LiveKit's ESP32 examples](https://github.com/livekit/client-sdk-esp32/tree/main/components/livekit/examples) cover the later on-device audio connection.


[Proposal](https://docs.google.com/document/d/1dz02PJORUFB1m--cltmt9tKI_VPXVcO9dAPNODxkFbo/edit) · [Work split](https://notes.granola.ai/t/a576ba7f-ef74-42ac-b631-dfefc260f884-008umkv4)
