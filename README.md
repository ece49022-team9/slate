# Slate

A portable voice assistant for everyday tasks. The ESP32 is a thin client: it captures the microphones, drives the display, and talks to the cloud over Wi-Fi. Everything else runs on Modal: speech, the agent, its tools, and the browser. React handles setup and approvals.

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

One Python backend, deployed by [server/slate/cloud.py](server/slate/cloud.py). Keep model calls with the agent and permission checks in access. The web app never gets provider secrets.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Node 24](https://nodejs.org/), then:

```sh
make setup
make agent-setup   # download and verify the pinned Hermes source
make agent-login   # authorize Hermes with your Codex subscription
make cloud-setup   # create the slate-cloud Modal secret and upload the Hermes login
make cloud-deploy  # build and deploy the Slate service
```

`make cloud-deploy` runs one Modal container with the Slate service, the Hermes gateway, the device MCP server, and the browser supervisor, and writes the service URL to `.local/cloud.json` with a generated device token. Do not commit `.local`. `make cloud-logs` follows the container's logs. Hermes keeps its home on the container's disk; its login, memories, sessions, skills, scheduled jobs, and databases are copied to the `slate-hermes` volume every five minutes and on shutdown, and restored on start. `make cloud-setup` uploads the local Hermes login only when the volume has none, so later runs keep the cloud's refreshed tokens.

The Mac runs only the simulator: `make sim` boots the firmware in QEMU, gives it a network through QEMU's Ethernet model, and provisions it with the URL and token from `.local/cloud.json`.

For the browser client, run `make web` with `VITE_SLATE_URL=wss://<service host>` and `VITE_SLATE_DEVICE_TOKEN` set, then open [localhost:5173](http://127.0.0.1:5173), connect your microphone, and hold Talk.

`make check` runs lint, formatting, tests and the web build.

## Agent

Slate owns the voice connection, cancellation, and approval UI. Hermes owns the tool loop, session search, and persistent memory. Hermes has web search and extraction, image analysis, a terminal, files and Python code execution (all in a Modal sandbox), skills, task planning, delegated agents, scheduled jobs, account connections, memory, past-chat search, the browser, and the device SDK. Its clarifying-question tool has no channel through the API server, so Slate asks follow-up questions in its spoken reply. The `slate-guard` plugin in [hermes/slate-guard](hermes/slate-guard) sends any tool that would send a message or spend money to Slate's approval prompt; browser clicks are judged by the label of the element they hit, and Enter on a checkout page or a typed card number also needs approval. `uv run python scripts/check_agent.py --tools` checks web search, the Modal terminal, and a denied purchase on a test shop. Its source and dependencies are pinned in [experiments/agent.toml](experiments/agent.toml); the cloud image builds from the same verified source archive as the local `.local/hermes` cache.

The cloud container starts our own headless Chromium in a Modal sandbox, with authenticated CDP. Each sandbox lasts four hours; the supervisor checks it every minute, replaces it when it stops, and writes the new address to Hermes's `browser.cdp_url`, which Hermes rereads without a restart. `make agent` and `make browser` still run the same setup on the Mac for local evaluations.

With the cloud service deployed:

```sh
make agent-check   # exact model/provider route and conversation recall
make agent-e2e     # browser form, cross-session memory, simulated spoken turn
make agent-status # latest receipts
```

The browser check reads a random code from a synthetic page, fills its form, and checks the resulting DOM independently. The memory check creates and removes a synthetic preference. The voice check feeds a shared WAV through QEMU's mic into the firmware, which streams it to the cloud over its own TLS WebSocket; the check reads the transcript, the reply, and the amount of speech the firmware received from its serial log. Reply WAVs and detailed traces stay in `.local/agent-runs`.

`make agent-managed` checks the managed OpenAI Agents API directly with its key from `doppler run`. Managed browser tests use OpenAI's hosted desktop; Hermes uses our Modal browser. These are capability checks, not a controlled harness ranking.

`make voice-profile` profiles three voice turns against the cloud with the shared spoken fixture, using a Python client that speaks the firmware's socket protocol. Run `make voice-deploy` after changing speech code. Receipts include connection and turn setup, STT queues and decoding, agent first visible text, CSM first acoustic frame and decoded PCM, and the first non-silent reply audio the client receives. `REPEATS=5` changes the sample count. Profiling is opt-in; normal speech uses the PCM streamer without CUDA timing.

The device's clock and the server's clock are never subtracted from each other. End-to-audible runs from the client's end message to the first non-silent reply PCM it receives, so it includes the network both ways; server spans start when the server receives end. Generation and playback overlap, so the report does not subtract whole generation spans to estimate latency without inference. Provider queueing, prefill, and reasoning remain combined within agent TTFT. CSM's first acoustic frame is not playable audio. Compare initial and loaded-model calls separately; profiling can perturb execution.

Short live voice turns pass, but some device-confirmation replies still reach CSM's generation limit without an end token. The voice turn reports an error even when its device commands were acknowledged. These checks do not establish speech-model reliability; failures and successful trials are recorded separately in the experiment log.

Hermes receives one `execute_device_code` MCP entry point by default. Hermes's tool search is turned off so the model sees every enabled tool's schema directly instead of looking it up first. Its Python REPL exposes `device.set_orb(color, radius)`, `device.show_text(text)`, and `device.get_status()`. Pydantic validates requests and firmware receipts; each active voice turn has a separate capability scope. Successful commands include matching request IDs and firmware revisions. Failed programs report already-applied actions, and their REPL state is discarded. Text is limited to 64 printable ASCII characters; radius is 10–45 pixels.

Monty runs as a bounded subprocess of the device MCP server inside the cloud container, next to the device route, so a device call never leaves the container until it reaches the device's socket. It exposes only the listed methods and has no host files, network, or shell access. It does not replace the Chromium/Linux sandbox.

`SLATE_DEVICE_MODE=modal make cloud-deploy` runs the same Python SDK in CPython 3.12 inside a Modal Sandbox with internet access. The sandbox calls the public device route itself, with the turn's scope as a bearer token; the scope stops working when the turn ends. Each scope gets a fresh interpreter in a warm sandbox; a spare is kept ready for the next voice turn. A timeout or cancellation terminates the whole sandbox. `SLATE_DEVICE_MODE=tools` selects the three separate MCP tools for comparison.

```sh
make device-check         # real firmware commands and OLED SPI pixels
make device-code-check    # Monty against QEMU through the HTTP device route
make device-code-profile  # time tools and Monty; LAYER=agent for local Hermes turns
```

The profile runs one composed task (set the orb, show text, read status) against QEMU firmware. `LAYER=exec` times each runtime in-process and through the MCP stdio boundary. `LAYER=modal` runs Monty inside a Modal container against an in-container fake device. `LAYER=agent` restarts the local Hermes once per mode in ABC/CBA order and times full turns with fresh sessions. Results append to the experiment log.

The code-runtime check replaces the device socket with a fixture and verifies actual firmware state, pixels, partial failures, and expired scopes. Pressing Talk while a reply is in progress interrupts it: the client drops queued speech immediately, and the server revokes device access and cancels transcription, agent, and speech work before starting another turn. This is button-triggered interruption; automatic speech detection is not implemented. Managed final-answer deltas feed sentence synthesis; Hermes waits for its final answer because its run stream does not preserve the phase labels needed to safely speak deltas. Both paths stream CSM's decoded PCM. CSM uses its pinned upstream sampling defaults; reaching the generation limit without an end token remains an error.

Replies start with one or two sentences to speak; anything after a line containing only `---` is shown but not spoken. Work that will take more than about a minute runs as a background Hermes subagent, and the spoken turn ends once it starts. Slate watches the Hermes session for the result, runs a follow-up turn when no one is talking, and speaks it; the browser shows it as an announcement. Turns have no fixed time limit: Talk interrupts them, and Hermes stops subagents that stop making progress. `check_agent.py --tools` also checks the spoken summary and a background task.

`SLATE_HARNESS=hermes|openai` selects the backend. `SLATE_MODEL` selects the requested model; `SLATE_PROVIDER` selects Hermes's provider. Authorize that provider in the Hermes profile before changing it, and set the same model/provider on Hermes and the Slate server. Each completed run checks the observed route and rejects an unexpected fallback. API-compatible providers can be tested through upstream Hermes; model/tool compatibility still needs its own evaluation.

This is a single-user experiment. A new conversation does not isolate Hermes's persistent memory from other conversations in the same profile. Account isolation and enrollment need separate profiles before this serves multiple people.

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

## Device connection

A device opens one WebSocket to `wss://<service>/api/device/socket` with `Authorization: Bearer <device token>`. Browsers, which cannot set that header, offer the subprotocols `slate` and the token instead. Control messages are JSON text frames; audio is binary 16-bit little-endian mono PCM.

| Direction | Message |
| --- | --- |
| Device → Slate | `{"type":"hello","rate":16000}` first; the server resamples to 24 kHz |
| Device → Slate | `{"type":"start"}`, binary mic frames, then `{"type":"end"}`; `{"type":"cancel"}` discards the turn |
| Device → Slate | `{"type":"live"}` starts a live call and `{"type":"hangup"}` ends it; mic frames stream the whole call |
| Device → Slate | `{"type":"receipt", ...}` for each command, with the firmware's state or an `error` |
| Slate → device | `turn`, `transcript` and `reply` (`text`, `final`), `tool`, `approval`, `error`, `cancelled`, each with `turn_id` |
| Slate → device | `{"type":"command","request_id","operation","arguments"}` for `set_orb`, `show_text`, `get_status` |
| Slate → device | `live` with `state` `started` or `ended` (`seconds`, maybe `error`), and `heard` and `said` word deltas, each with `call_id` |
| Slate → device | binary 24 kHz reply speech, paced about 0.3 s ahead of playback |

A live call runs GPT-Live in the service. GPT-Live listens the whole time, decides when the user has finished and when to stop talking, and hands questions and tasks to Hermes. Each handoff gets the words said since the previous one, plus the device scope and approvals of a turn of its own; GPT-Live speaks the part of Hermes's answer before `---`. If GPT-Live's connection drops, the call starts a new session seeded with the conversation so far. Background results arriving during a call are given to GPT-Live to say. Its voice prompt is [live.md](server/slate/voice/live.md), and it needs `OPENAI_API_KEY`, which `make cloud-setup` copies from Doppler into the Modal secret.

`GET /api/voice/reports`, with the device token, returns timing, usage, and words for the connected device's recent turns and live calls, including a call in progress. `SLATE_PROFILE=1` on the service adds speech-model timings to turn reports.

One device is connected at a time; a new connection replaces the old one. Recordings are limited to two minutes. To send a recording as a device, without QEMU:

```sh
uv run python -m slate.voice simulate .local/voice-check.wav
```

It reads a mono 24 kHz, 16-bit WAV, streams it at 16 kHz in real time, prints the transcript and answer, and saves the returned speech to `.local/reply.wav`.

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

The firmware reaches the cloud on its own. Provisioning is stored in flash and sent over serial: `!net wifi <ssid> <password>` on the board or `!net eth` in QEMU, then `!cloud <wss URL> <device token>`. `make provision SSID=<name> PASSWORD=<password>` sends both to a connected board using `.local/cloud.json`; names and passwords with spaces are not supported yet. Above the network driver the code is the same on both: TLS pinned to the ISRG roots, the WebSocket client, and the protocol above, in its own FreeRTOS task. Entering listen sends `start` and streams the selected mic; leaving it for transcribe sends `end`. The firmware logs `slate.transcript:`, `slate.reply:`, and `slate.reply.audio:`; it has no speaker yet, so it counts reply speech and drops it.

The mic schematic has two MP34DT01-M parts that share PDM clock and data: MK1 has L/R grounded (left slot) and MK2 has L/R tied high (right slot). The breadboard has one mic for now, on the left slot. I2S0 turns both slots into interleaved 16 kHz PCM. The firmware picks left, right, or an even mix, removes DC, and queues up to one second of audio. Mute and cancel clear the queue and the filter; transcribe keeps the queue so it can drain. The orb grows and brightens with the mic level while listening.

### Board description

[hardware/board.toml](hardware/board.toml) says which part is wired to which pin. [hardware/parts.toml](hardware/parts.toml) lists the parts we can use, with inventory counts from the class sheet, pin limits, supply range, and datasheet timing. `make board` checks the wiring and writes [firmware/main/board.h](firmware/main/board.h); `make firmware`, `make flash`, and `make check` run the same check. It rejects pins the chip lacks, input-only pins driving a part, flash/PSRAM/USB pins, shared pins, supply mismatches, and a PDM clock outside the mic's range. It warns about boot pins, SPI clocks above the datasheet limit, and wires long enough to ring (`wire_cm` on a device). To try another board, change `mcu`; `esp32-wrover-e`, for example, fails because its PSRAM uses GPIO16/17.

Current wiring: OLED clock GPIO18, MOSI GPIO23, CS GPIO5, D/C GPIO16, reset GPIO17 at 16 MHz; PDM clock GPIO26 and data GPIO36. GPIO39 is input-only on the ESP32 and cannot drive the PDM clock. The SSD1351 datasheet lists 4.5 MHz as its SPI limit; the panel works at 16 MHz on the bench, so the checker only warns.

### Simulator

`make sim-setup` builds Espressif's QEMU with [sim/qemu.patch](sim/qemu.patch). The patch adds working GPIO output registers, an I2S0 receiver with DMA for the PDM mic, and a bridge to Python. It also fixes a QEMU bug that sent a stray command byte before every SPI transfer, which byte-swapped the OLED's pixels, and holds each SPI transfer busy for the bits it clocks out at the programmed rate, so a full OLED frame takes about 18 ms at 16 MHz as it does on the bus.

QEMU counts time by instructions (4 ns each, close to the ESP32's 240 MHz) instead of following the Mac's clock. The bridge sends pin changes, SPI bytes, and serial output to Python in order, and every emulated millisecond it stops and waits. Python answers with any serial keys or mic audio due at that moment, then lets it continue. Tests wait on emulated time, so the same inputs give the same run: `check_breadboard.py` runs twice and requires identical serial output and identical frames. The checks run as fast as the Mac allows; `make sim` and `make sim-voice` pace emulated time to the wall clock. QEMU emulates no Wi-Fi radio, so the simulated board uses QEMU's OpenCores Ethernet model, and QEMU's user networking sends its traffic through the Mac.

QEMU boots the exact image `make flash` writes. Python plays the parts on the breadboard: an SSD1351 that decodes the Adafruit library's SPI commands into display memory, and a mic that feeds PCM into the slot set by `slot` in `board.toml`. The host talks to the emulated serial port the same way it would talk to the board.

`make sim-check` runs these checks against that image:

- [check_breadboard.py](scripts/check_breadboard.py): two identical runs; the idle orb is drawn; a 440 Hz tone on the mic's slot reaches the meter within 5% RMS; the other slot stays silent; the orb grows and brightens while it hears sound; the panel runs at about 33 fps.
- [check_mic.py](scripts/check_mic.py): Hypothesis generates stereo audio for each channel. The streamed output must match an independent DC-filter calculation within one step, replay identically, and start clean after a muted turn. Failures are shrunk and saved for replay.
- [check_display.py](scripts/check_display.py): all six state colors, a blank panel on mute, a centered orb that fits the panel, smooth breathing, and 15 generated state sequences. It saves a frame to `.local/display-listen.png`.
- [check_resources.py](scripts/check_resources.py): while listening to a tone, the firmware's `slate.perf` report must show a frame (render plus SPI) within 30 ms, each 20 ms audio block processed in time, at least 32 KB of heap left with a 16 KB free block, and 512 bytes of stack left in every task.

QEMU runs the same 520 KB SRAM map, the WROOM-32's 4 MB flash, and no PSRAM, so heap and stack limits are the real ones. Its CPU is not cycle-accurate: every instruction costs 4 ns, with no flash cache misses or wait states. `make calibrate-sim` runs the same workload on the board and in QEMU and writes the hardware/QEMU timing ratios to `firmware/sim_calibration.toml`; the simulator check scales its timings by those ratios. Until then it reports timing as uncalibrated. `make bench` applies the same budgets to the board's own report.

`make sim` serves a breadboard view at [localhost:8010](http://127.0.0.1:8010). It draws the board and parts from `board.toml` with the wiring checker's warnings, animates each wire when its pin toggles or SPI bytes flow, and shows the live panel and the serial console. You can type keys, switch states, play a 440 Hz tone into the mic, or stream your computer's microphone into it. The Voice panel starts a call from the firmware: always listening sends `c` for a live call, and push to talk holds Listen while you hold Talk. Both turn on the audio tap (`a`), which copies the mic frames and the reply speech the firmware receives to serial, so the page plays Slate's voice. Echo feeds that speech back into the simulated mics, quieter and 40 ms late, the way a speaker beside the mics would. Serial output goes to `.local/board-serial.log` and QEMU's own output to `.local/qemu.log`.

To send audio through the simulated firmware, the cloud agent, and the real speech services, run:

```sh
make sim-voice
```

Pass `INPUT=path/to/speech.wav` to use a different recording. The WAV must be 16-bit at 16, 24, or 48 kHz and at most 119 seconds. A stereo WAV fills both mic slots; a mono WAV fills the board's mic slot. `--channel` on the underlying Python command picks left (default), right, or mix. The command boots QEMU, waits for the firmware to connect to the cloud, plays the WAV into its mic model while holding listen, and prints the transcript, the reply, and how much speech the firmware received. Haptics are not started until their pins are confirmed.

### Bench

`make bench` flashes the board and runs the hardware version of the simulator checks over USB serial. It steps through all six states, reads the frame rate the board reports, records a second of background sound, then plays a 440 Hz tone through the Mac's speaker. The mic's streamed audio must be at least three times louder than the background, and 440 Hz must be at least ten times stronger than 300 Hz, 600 Hz, 1 kHz, and 2 kHz. Put the board near the speaker and turn the volume up. The script cannot see the panel, so check by eye that the orb changes color with each state. Serial output goes to `.local/bench-serial.log`.

To run the same turn on the real board after `make provision`, add `--port`; the Mac plays the WAV aloud and the board streams what its mic hears over Wi-Fi:

```sh
MODAL_PROFILE=sudarshan-1 uv run python -m slate.voice firmware .local/voice-check.wav --port /dev/cu.usbserial-2120
```

[Proposal](https://docs.google.com/document/d/1dz02PJORUFB1m--cltmt9tKI_VPXVcO9dAPNODxkFbo/edit) · [Work split](https://notes.granola.ai/t/a576ba7f-ef74-42ac-b631-dfefc260f884-008umkv4)
