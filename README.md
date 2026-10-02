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

Run `make livekit`, `make agent`, `make server`, and `make web` in separate terminals after the agent setup below.

Open [localhost:5173](http://127.0.0.1:5173), connect your microphone, and hold Talk. Release it to finish; Cancel discards the recording. The page shows the transcript and Slate's reply and plays the reply over LiveKit.

`make check` runs lint, formatting, tests and the web build. [API docs](http://127.0.0.1:8000/api/docs) come from the backend.

## Agent

Slate owns the voice connection, cancellation, and approval UI. Hermes owns the tool loop, session search, and persistent memory. Its source and dependencies are pinned in [experiments/agent.toml](experiments/agent.toml); upstream code lives in the ignored `.local/hermes` cache.

```sh
make agent-setup
make agent-login
make browser
make agent
```

`make agent-login` authorizes a private Hermes profile. Credentials and memory stay in `.local/hermes-home`; do not commit it. `make browser` starts our own headless Chromium in a Modal sandbox, with authenticated CDP. Start the browser before Hermes, or restart Hermes after creating a new sandbox. `make browser-stop` terminates it. The sandbox expires after 30 minutes; the command reports errors rather than silently selecting another browser.

With LiveKit and the Slate server running:

```sh
make agent-check   # exact model/provider route and conversation recall
make agent-e2e     # browser form, cross-session memory, simulated spoken turn
make agent-status # latest receipts
```

The browser check reads a random code from a synthetic page, fills its form, and checks the resulting DOM independently. The memory check creates and removes a synthetic preference. The voice check feeds a shared WAV through QEMU's mic, the firmware's PCM stream, LiveKit, Kyutai, the agent, CSM, and the returned LiveKit audio. Reply WAVs and detailed traces stay in `.local/agent-runs`.

To compare the managed OpenAI Agents API, run `make server-managed` in another terminal, then `make agent-managed`. The server uses port 8001 and obtains its key with `doppler run`. Managed browser tests use OpenAI's hosted desktop; Hermes uses our Modal browser. These are capability checks, not a controlled harness ranking. The voice comparison reuses the same input WAV.

`SLATE_HARNESS=hermes|openai` selects the backend. `SLATE_MODEL` selects the requested model; `SLATE_PROVIDER` selects Hermes's provider. Authorize that provider in the Hermes profile before changing it, and set the same model/provider on Hermes and the Slate server. For an API-key provider, start Hermes with `doppler run -- make agent` and those environment variables. Each completed run checks the observed route and rejects an unexpected fallback. API-compatible providers can be tested through upstream Hermes; model/tool compatibility still needs its own evaluation.

This is a local, single-user experiment. A new conversation does not isolate Hermes's persistent memory from other conversations in the same profile. Account isolation and enrollment need separate profiles before this serves multiple people.

## Evaluations

[experiments/progress.jsonl](experiments/progress.jsonl) is the append-only experiment log. It includes failures, source revisions, protocol identifiers, and denominators. Raw traces are private and ignored. Smoke checks and benchmark scores have separate scopes.

We use two external suites:

- [Sierra's τ benchmark](https://github.com/sierra-research/tau2-bench): policy-following, multi-turn task execution, and verified tool/state outcomes. The pinned repository now calls its protocol τ³; start with its airline text tasks. Retail references need auditing because upstream issue 499 reports incorrect expected results.
- [LongMemEval](https://github.com/xiaowu0162/LongMemEval): recall across timestamped conversations, updates, reasoning, and abstention. Use the cleaned V1 S dataset's complete histories with an isolated Hermes profile per question. The session-search arm uses Hermes's native search; full-context and no-memory arms show the retrieval gap. This pilot does not test learning memories through natural conversations.

```sh
make eval-setup
make eval-memory LIMIT=3
make eval-tau
make eval-tau-audit
```

Memory runs freeze the sample, dataset, model, source, and judge. They use the pinned upstream answer rubric with a GPT-6.1 Sol Responses judge; this is a custom judge protocol, not an official leaderboard result. `make eval-tau` runs the same seeded airline task through Hermes's MCP tools and the managed Agents API's function handlers, with the official environment and outcome evaluator. `TASK=18` selects the task; the default is one seeded sample, one trial per harness. `make eval-tau-audit` separately audits all airline gold references. Small pilots establish the pipeline and expose failures; they do not establish performance on the full suites or pass^4 reliability.

## Microphone connection

The browser sends a LiveKit microphone track. The backend converts it to mono 24 kHz PCM, streams it to Kyutai, sends the final transcript to the agent, and speaks its answer with CSM. The agent defaults to pinned upstream Hermes with GPT-6.1 Sol through your Codex subscription. The managed OpenAI Agents API is a separate comparison path using `OPENAI_API_KEY` through Doppler. To use a recording as the device:

```sh
uv run python -m slate.voice simulate .local/voice-check.wav
```

The simulator reads a mono 24 kHz, 16-bit WAV and publishes 16 kHz audio by default. `--sample-rate 48000` tests a browser-rate stream. It uses the same room, audio track, and controls as the browser. It prints the transcript and answer and saves LiveKit's returned speech to `.local/reply.wav`.

The firmware will use the same contract:

| Operation | Interface |
| --- | --- |
| Connect | `POST /api/voice/sessions` with `{}` returns a room URL, participant token, and worker identity |
| Send audio | Publish one LiveKit microphone track |
| Start | Call the worker's `start_turn` RPC; keep the returned turn ID |
| Finish | Stop sending audio, then call `end_turn` with that ID |
| Cancel | Call `cancel_turn` with that ID |
| Read text | Receive `slate.transcript` packets: `{turn_id, text, final}` or `{turn_id, error}` |
| Read reply | Receive `slate.reply` packets and subscribe to the worker's audio track |

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

The target is the original ESP32, on an ESP32-WROOM-32 dev board. One PlatformIO build runs both on the board and in QEMU:

```sh
make firmware   # build
make flash      # write it to the board
make monitor    # serial console at 921600 baud
make sim-setup  # build the patched QEMU, once
make sim        # run the same image in QEMU with a browser preview
make sim-check  # run the simulator checks
make bench      # flash the board and run the hardware checks
```

`make flash` and `make monitor` pick the board automatically when exactly one USB serial port is present. If several are connected, run `make ports` and pass `PORT=/dev/cu.<port>`. Over serial, send `0` through `5` for idle, listen, mute, transcribe, respond, or error; `l`, `r`, or `m` (while idle) to pick the left mic, right mic, or their mix; and `a` or `x` to turn audio streaming on or off. In listen, the firmware prints the mic's sample count, RMS, and peak every half second. With streaming on, it also sends the filtered mic audio as binary frames: a zero byte, a two-byte little-endian length, then 16-bit samples. Text never contains a zero byte, so the host can tell them apart. The ROM's boot messages use 115200 baud and look garbled at 921600.

The mic schematic has two MP34DT01-M parts that share PDM clock and data: MK1 has L/R grounded (left slot) and MK2 has L/R tied high (right slot). The breadboard has one mic for now, on the left slot. I2S0 turns both slots into interleaved 16 kHz PCM. The firmware picks left, right, or an even mix, removes DC, and queues up to one second of audio. Mute and cancel clear the queue and the filter; transcribe keeps the queue so it can drain. The orb grows and brightens with the mic level while listening.

### Board description

[hardware/board.toml](hardware/board.toml) says which part is wired to which pin. [hardware/parts.toml](hardware/parts.toml) lists the parts we can use, with inventory counts from the class sheet, pin limits, supply range, and datasheet timing. `make board` checks the wiring and writes [firmware/main/board.h](firmware/main/board.h); `make firmware`, `make flash`, and `make check` run the same check. It rejects pins the chip lacks, input-only pins driving a part, flash/PSRAM/USB pins, shared pins, supply mismatches, and a PDM clock outside the mic's range. It warns about boot pins, SPI clocks above the datasheet limit, and wires long enough to ring (`wire_cm` on a device). To try another board, change `mcu`; `esp32-wrover-e`, for example, fails because its PSRAM uses GPIO16/17.

Current wiring: OLED clock GPIO18, MOSI GPIO23, CS GPIO5, D/C GPIO16, reset GPIO17 at 16 MHz; PDM clock GPIO26 and data GPIO36. GPIO39 is input-only on the ESP32 and cannot drive the PDM clock. The SSD1351 datasheet lists 4.5 MHz as its SPI limit; the panel works at 16 MHz on the bench, so the checker only warns.

### Simulator

`make sim-setup` builds Espressif's QEMU with [sim/qemu.patch](sim/qemu.patch). The patch adds working GPIO output registers, an I2S0 receiver with DMA for the PDM mic, and a bridge to Python. It also fixes a QEMU bug that sent a stray command byte before every SPI transfer, which byte-swapped the OLED's pixels.

QEMU counts time by instructions (4 ns each, close to the ESP32's 240 MHz) instead of following the Mac's clock. The bridge sends pin changes, SPI bytes, and serial output to Python in order, and every emulated millisecond it stops and waits. Python answers with any serial keys or mic audio due at that moment, then lets it continue. Tests wait on emulated time, so the same inputs give the same run: `check_breadboard.py` runs twice and requires identical serial output and identical frames. The checks run as fast as the Mac allows; `make sim` and the LiveKit command pace emulated time to the wall clock.

QEMU boots the exact image `make flash` writes. Python plays the parts on the breadboard: an SSD1351 that decodes the Adafruit library's SPI commands into display memory, and a mic that feeds PCM into the slot set by `slot` in `board.toml`. The host talks to the emulated serial port the same way it would talk to the board.

`make sim-check` runs three checks against that image:

- [check_breadboard.py](scripts/check_breadboard.py): two identical runs; the idle orb is drawn; a 440 Hz tone on the mic's slot reaches the meter within 5% RMS; the other slot stays silent; the orb grows and brightens while it hears sound; the panel runs at about 33 fps.
- [check_mic.py](scripts/check_mic.py): Hypothesis generates stereo audio for each channel. The streamed output must match an independent DC-filter calculation within one step, replay identically, and start clean after a muted turn. Failures are shrunk and saved for replay.
- [check_display.py](scripts/check_display.py): all six state colors, a blank panel on mute, a centered orb that fits the panel, smooth breathing, and 15 generated state sequences. It saves a frame to `.local/display-listen.png`.

`make sim` serves a breadboard view at [localhost:8010](http://127.0.0.1:8010). It draws the board and parts from `board.toml` with the wiring checker's warnings, animates each wire when its pin toggles or SPI bytes flow, and shows the live panel and the serial console. You can type keys, switch states, play a 440 Hz tone into the mic, or stream your computer's microphone into it. Serial output goes to `.local/board-serial.log` and QEMU's own output to `.local/qemu.log`.

To send audio through the simulated firmware, agent, and real speech services, start `make livekit` and `make server`, then run:

```sh
make sim-voice
```

Pass `INPUT=path/to/speech.wav` to use a different recording. The WAV must be 16-bit at 16, 24, or 48 kHz and at most 119 seconds. A stereo WAV fills both mic slots; a mono WAV fills the board's mic slot. `--channel` on the underlying Python command picks left (default), right, or mix. The command boots QEMU, plays the WAV into its mic model, reads the firmware's streamed audio, and publishes it as a LiveKit microphone track. It prints the transcript and agent reply and saves the received CSM speech to `.local/reply.wav`. The reply is heard by the host LiveKit client; the firmware has no speaker output path yet. The same serial mic stream works on the real board, but the firmware does not connect to LiveKit over Wi-Fi yet. [LiveKit's ESP32 examples](https://github.com/livekit/client-sdk-esp32/tree/main/components/livekit/examples) cover that later step. Haptics are not started until their pins are confirmed.


### Bench

`make bench` flashes the board and runs the hardware version of the simulator checks over USB serial. It steps through all six states, reads the frame rate the board reports, records a second of background sound, then plays a 440 Hz tone through the Mac's speaker. The mic's streamed audio must be at least three times louder than the background, and 440 Hz must be at least ten times stronger than 300 Hz, 600 Hz, 1 kHz, and 2 kHz. Put the board near the speaker and turn the volume up. The script cannot see the panel, so check by eye that the orb changes color with each state. Serial output goes to `.local/bench-serial.log`.

To send speech from the real board to LiveKit, add `--port`; the Mac plays the WAV aloud and the board streams what its mic hears:

```sh
MODAL_PROFILE=sudarshan-1 uv run python -m slate.voice firmware .local/voice-check.wav --port /dev/cu.usbserial-2120
```

[Proposal](https://docs.google.com/document/d/1dz02PJORUFB1m--cltmt9tKI_VPXVcO9dAPNODxkFbo/edit) · [Work split](https://notes.granola.ai/t/a576ba7f-ef74-42ac-b631-dfefc260f884-008umkv4)
