# Slate

A portable voice assistant for everyday tasks. The ESP32 handles audio and device feedback. Python handles speech, model calls, and browser tasks. React handles setup and approvals.

## Code

| Path | Work |
| --- | --- |
| `firmware/` | ESP32 firmware |
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

The target is the original ESP32, using the `esp32dev` profile for an ESP32-WROOM-32 dev board. Install ESP-IDF 5.5.5 and Espressif's QEMU, then build and boot it headlessly:

```sh
make firmware-setup
make firmware-check
```

`make firmware` builds. `make firmware-sim` leaves the serial console open. Exit with Ctrl-A, then X. The setup uses `~/esp/esp-idf-v5.5.5` and `~/.espressif`, with Python packages managed by uv.

QEMU runs the same FreeRTOS state controller, channel selection, DC filter, and microphone buffer as the board build. `make firmware-check` checks capture gating, left/right/mix selection, overflow rejection, and buffer reset. `make firmware-mic-check` uses Hypothesis to generate 60 stereo audio cases, replay them with different chunk boundaries, and compare the firmware output against an independent PCM calculation. Failures are shrunk and saved for replay.

The microphone schematic contains two MP34DT01-M parts: MK1 has L/R grounded and MK2 has L/R tied high. They share PDM clock/data. The ESP32 driver uses I2S0 to convert both PDM slots to interleaved 16 kHz PCM. The shared pipeline selects left, right, or an equal-weight mono mix, removes DC, and queues up to one second of audio. Overflow rejects a whole frame; mute/cancel clears queued samples and filter history, while finishing a turn preserves buffered audio for draining. Gain is unity; the old fixed 4x amplification is removed to avoid clipping recorded speech.

To send simulated microphone audio through the firmware and the real transcription service, start `make livekit` and `make server`, then run:

```sh
MODAL_PROFILE=sudarshan-1 uv run python -m slate.voice firmware .local/voice-check.wav
```

Use a 16-bit WAV at 16, 24, or 48 kHz, up to 119 seconds. Stereo WAVs represent the two microphone slots; mono WAVs populate only the left slot. `--channel left` is the default; `--channel right` selects the other mic and `--channel mix` averages both. For a mono WAV, mix therefore halves the signal level and right produces silence.

The command builds and boots QEMU, injects stereo PCM at the boundary after hardware PDM conversion, reads processed mono audio from the firmware buffer through UART1, and publishes it as a LiveKit microphone track. The existing start/end-turn RPCs and transcription service handle the recording. A host bridge runs WebRTC; this does not implement on-device LiveKit networking or emulate PDM clock edges, DMA, or physical microphones. QEMU has no I2S/PDM emulation.

`make firmware-hardware` compiles the ESP32-WROOM-32 PDM adapter and peripheral drivers with pinned PlatformIO dependencies. To flash the dev board over USB, list ports and use the one that appears when it is connected:

```sh
make flash
make monitor
```

`make flash` and `make monitor` select the board automatically when exactly one matching USB serial port is present. If several are connected, run `make ports` and pass `PORT=/dev/cu.<port>` explicitly. The monitor runs at 115200 baud; send `0` through `5` to request idle, listen, mute, transcribe, respond, or error and inspect the `slate.state` logs. PDM clock is GPIO26 and shared PDM data is GPIO36. GPIO39 is input-only on the original ESP32 and cannot drive the PDM clock. Send `1` to start capture and read the half-second `slate.mic` sample count, RMS, and peak reports. Speak near the mics: in LISTEN, the green orb should expand and brighten toward white. Return to idle with `0`, then send `l`, `r`, or `m` to select the left mic, right mic, or mix before listening again. The serial meter drains audio for this board test; the firmware does not yet send physical microphone audio to LiveKit. Haptics are not started until their pin mapping is verified. [LiveKit's ESP32 examples](https://github.com/livekit/client-sdk-esp32/tree/main/components/livekit/examples) cover the later on-device audio connection.


### Board description and breadboard simulation

[hardware/board.toml](hardware/board.toml) says which part is wired to which pin. [hardware/parts.toml](hardware/parts.toml) lists the parts we can use, with inventory counts from the class sheet, pin limits, supply range, and datasheet timing. `make board` checks the wiring and writes [firmware/main/board.h](firmware/main/board.h); `make flash` and `make check` run the same check. It rejects pins the chip lacks, input-only pins driving a part, flash/PSRAM/USB pins, shared pins, supply mismatches, and a PDM clock outside the mic's range. It warns about boot pins, SPI clocks above the datasheet limit, and wires long enough to ring (`wire_cm` on a device). To try another board, change `mcu`; for example, `esp32-wrover-e` fails because its PSRAM uses GPIO16/17.

`make sim-setup` builds Espressif's QEMU with [sim/qemu.patch](sim/qemu.patch). The patch adds working GPIO output registers, forwards pin changes and VSPI bytes (with emulator time) to Python, and fixes a QEMU bug that sent a stray command byte before every SPI transfer. `make sim-check` boots the same image `make flash` writes, decodes the Adafruit library's SPI traffic with an SSD1351 model, and checks the panel color against the state printed on serial and the frame rate. QEMU does not emulate I2S yet, so the PDM driver times out and the firmware reports ERROR; that is the next piece.

### Display preview

The display is Adafruit product 1431: a 128×128 RGB565 OLED with an SSD1351 controller. Run:

```sh
make firmware-display
```

Open [localhost:8010](http://127.0.0.1:8010). The preview reads pixel frames from the firmware running in QEMU. Its buttons request real controller state changes: idle is white, listen green, mute black, transcribe blue, respond yellow, and error magenta. A centered orb glows with a soft halo and expands/contracts sinusoidally over a 120-frame cycle (3.6 seconds on the firmware clock). In LISTEN, captured audio adds size and shifts its color from green toward white. The browser preview does not capture computer microphone audio.

`display.cpp` renders the same RGB565 pixel buffer in both builds. The board's `oled.cpp` sends it through Adafruit's SSD1351 library; QEMU returns it over the simulator connection for the browser to display. The Arduino library's SPI commands and the panel electronics are not emulated. The board build uses the confirmed OLED wiring: clock GPIO18, MOSI GPIO23, CS GPIO5, D/C GPIO16, and reset GPIO17. These five ESP32 pins are defined as `SLATE_OLED_CLK`, `SLATE_OLED_DATA`, `SLATE_OLED_CS`, `SLATE_OLED_DC`, and `SLATE_OLED_RESET`. Hardware SPI now sends the full frame at 16 MHz. The OLED task reports its measured frame rate every two seconds; the connected board reported 33.3 fps after the change.

`make firmware-display-check` checks all six state colors, blanking, animation geometry, audio response, and 30 generated frame/state combinations. It saves an actual QEMU frame to `.local/display-listen.png`. Each simulator command starts its own QEMU instance; stop the preview before running the audio or display checks, since they use the same flash image. The preview controls its own simulated device and does not mirror a separate microphone simulator process.


[Proposal](https://docs.google.com/document/d/1dz02PJORUFB1m--cltmt9tKI_VPXVcO9dAPNODxkFbo/edit) · [Work split](https://notes.granola.ai/t/a576ba7f-ef74-42ac-b631-dfefc260f884-008umkv4)
