.PHONY: setup server web check board firmware flash monitor ports sim-setup sim sim-check sim-voice bench voice-deploy voice-check livekit agent-setup agent-login agent browser browser-stop agent-check agent-e2e agent-managed agent-status server-managed eval-setup eval-memory eval-tau eval-tau-audit voice-profile server-duplex duplex-profile cost-deploy cost-profile

MODAL_PROFILE ?= sudarshan-1
MODAL_ENVIRONMENT ?= main
INPUT ?= .local/voice-check.wav
ESP32_PORTS := $(wildcard /dev/cu.usbserial* /dev/cu.wchusbserial* /dev/cu.SLAB_USBtoUART*)
PORT ?= $(if $(filter 1,$(words $(ESP32_PORTS))),$(firstword $(ESP32_PORTS)))

setup:
	uv sync --locked
	npm --prefix web ci

server:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run uvicorn slate.app:app --reload --host 127.0.0.1 --port 8000

web:
	npm --prefix web run dev

check:
	uv run python -m slate.board --check
	uv run ruff check server scripts tests
	uv run ruff format --check server scripts tests
	uv run python -m unittest discover -s tests
	npm --prefix web run lint
	npm --prefix web run build

board:
	uv run python -m slate.board

firmware: board
	uv tool run --from platformio==6.2.0 pio run -d firmware

ports:
	uv tool run --from platformio==6.2.0 pio device list

flash: board
	@test -n "$(PORT)" || (printf 'Expected one ESP32 serial port; found: %s. Run make ports, then make flash PORT=/dev/cu.<port>\n' "$(ESP32_PORTS)" >&2; exit 1)
	uv tool run --from platformio==6.2.0 pio run -d firmware -t upload --upload-port "$(PORT)"

monitor:
	@test -n "$(PORT)" || (printf 'Expected one ESP32 serial port; found: %s. Run make ports, then make monitor PORT=/dev/cu.<port>\n' "$(ESP32_PORTS)" >&2; exit 1)
	uv tool run --from platformio==6.2.0 pio device monitor -d firmware -p "$(PORT)" -b 921600

bench: flash
	uv run python scripts/check_bench.py "$(PORT)"

sim-setup:
	bash scripts/qemu.sh

sim:
	uv run python -m slate.display

sim-check:
	uv run python scripts/check_breadboard.py
	uv run python scripts/check_mic.py
	uv run python scripts/check_display.py

sim-voice:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.voice firmware "$(INPUT)"

voice-deploy:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run modal deploy -m slate.voice.stt
	MODAL_PROFILE=$(MODAL_PROFILE) uv run modal deploy -m slate.voice.tts

voice-check:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.voice check

livekit:
	livekit-server --dev --bind 127.0.0.1 --node-ip 127.0.0.1

agent-setup:
	uv run python -m slate.agent.runtime setup

agent-login:
	uv run python -m slate.agent.runtime login

agent:
	uv run python -m slate.agent.runtime start

browser:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.browser.modal start

browser-stop:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.browser.modal stop

agent-check:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python scripts/check_agent.py

agent-status:
	uv run python scripts/check_agent.py --status

voice-profile:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python scripts/profile_voice.py --repeats $(or $(REPEATS),3)

server-duplex:
	MODAL_PROFILE=$(MODAL_PROFILE) MODAL_ENVIRONMENT=$(MODAL_ENVIRONMENT) doppler run -- uv run uvicorn slate.app:app --host 127.0.0.1 --port 8002

duplex-profile:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m scripts.profile_live --repeats $(or $(REPEATS),4) --interruptions $(or $(INTERRUPTIONS),3)

cost-deploy:
	MODAL_PROFILE=$(MODAL_PROFILE) MODAL_ENVIRONMENT=slate-cost uv run modal deploy -m slate.voice.stt
	MODAL_PROFILE=$(MODAL_PROFILE) MODAL_ENVIRONMENT=slate-cost uv run modal deploy -m slate.voice.tts

cost-profile:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -u -m scripts.profile_cost

agent-e2e:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python scripts/check_agent.py --browser --memory --voice

server-managed:
	SLATE_HARNESS=openai MODAL_PROFILE=$(MODAL_PROFILE) doppler run -- uv run uvicorn slate.app:app --host 127.0.0.1 --port 8001

agent-managed:
	doppler run -- uv run python scripts/check_managed.py --browser
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python scripts/check_agent.py --voice-only --api-url http://127.0.0.1:8001 --harness openai

eval-setup:
	uv run python scripts/eval_agent.py setup

eval-memory:
	doppler run -- uv run python scripts/eval_agent.py longmemeval --limit $(or $(LIMIT),3) --grade

eval-tau:
	doppler run -- uv run python scripts/eval_agent.py tau --task-id $(or TASK,18)

eval-tau-audit:
	uv run python scripts/eval_agent.py tau-audit
