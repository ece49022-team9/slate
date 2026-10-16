.PHONY: setup cloud-setup cloud-deploy cloud-logs web check board firmware flash provision monitor ports sim-setup sim sim-check sim-voice bench calibrate-sim voice-deploy voice-check agent-setup agent-login agent browser browser-stop agent-check agent-e2e agent-managed agent-status eval-setup eval-memory eval-tau eval-tau-audit voice-profile device-check device-code-check device-code-profile

MODAL_PROFILE ?= sudarshan-1
INPUT ?= .local/voice-check.wav
ESP32_PORTS := $(wildcard /dev/cu.usbserial* /dev/cu.wchusbserial* /dev/cu.SLAB_USBtoUART*)
PORT ?= $(if $(filter 1,$(words $(ESP32_PORTS))),$(firstword $(ESP32_PORTS)))

setup:
	uv sync --locked
	npm --prefix web ci

cloud-setup:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.cloud setup

cloud-deploy:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.cloud deploy

cloud-logs:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run modal app logs slate

web:
	npm --prefix web run dev

check:
	uv run python -m slate.board --check
	uv run ruff check server scripts tests hermes
	uv run ruff format --check server scripts tests hermes
	uv run python -m unittest discover -s tests
	npm --prefix web run lint
	npm --prefix web run build
	node --experimental-strip-types --test web/tests/*.test.mjs

board:
	uv run python -m slate.board

firmware: board
	uv tool run --from platformio==6.2.0 pio run -d firmware

ports:
	uv tool run --from platformio==6.2.0 pio device list

flash: board
	@test -n "$(PORT)" || (printf 'Expected one ESP32 serial port; found: %s. Run make ports, then make flash PORT=/dev/cu.<port>\n' "$(ESP32_PORTS)" >&2; exit 1)
	uv tool run --from platformio==6.2.0 pio run -d firmware -t upload --upload-port "$(PORT)"

provision:
	@test -n "$(PORT)" || (printf 'Expected one ESP32 serial port; found: %s. Run make ports, then make provision PORT=/dev/cu.<port>\n' "$(ESP32_PORTS)" >&2; exit 1)
	@test -n "$(SSID)" || (printf 'Pass SSID=<wifi name> PASSWORD=<wifi password>\n' >&2; exit 1)
	uv run python scripts/provision.py "$(PORT)" --ssid "$(SSID)" --password "$(PASSWORD)"

monitor:
	@test -n "$(PORT)" || (printf 'Expected one ESP32 serial port; found: %s. Run make ports, then make monitor PORT=/dev/cu.<port>\n' "$(ESP32_PORTS)" >&2; exit 1)
	uv tool run --from platformio==6.2.0 pio device monitor -d firmware -p "$(PORT)" -b 921600

bench: flash
	uv run python scripts/check_bench.py "$(PORT)"
	uv run python scripts/check_resources.py --port "$(PORT)"

calibrate-sim: flash
	uv run python scripts/check_resources.py --port "$(PORT)" --calibrate

sim-setup:
	bash scripts/qemu.sh

sim:
	uv run python -m slate.display

sim-check:
	uv run python scripts/check_breadboard.py
	uv run python scripts/check_mic.py
	uv run python scripts/check_display.py
	uv run python scripts/check_device.py
	uv run python scripts/check_resources.py
	uv run python scripts/check_cloud_link.py

device-check:
	uv run python scripts/check_device.py

device-code-check:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python scripts/check_device_code.py

device-code-profile:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python scripts/profile_device_code.py --layer $(or $(LAYER),exec)

sim-voice:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.voice firmware "$(INPUT)"

voice-deploy:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run modal deploy -m slate.voice.stt
	MODAL_PROFILE=$(MODAL_PROFILE) uv run modal deploy -m slate.voice.tts

voice-check:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.voice check

agent-setup:
	uv run python -m slate.agent.runtime setup

agent-login:
	uv run python -m slate.agent.runtime login

agent:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.agent.runtime start

browser:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.browser.modal run

browser-stop:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.browser.modal stop

agent-check:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python scripts/check_agent.py

agent-status:
	uv run python scripts/check_agent.py --status

voice-profile:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python scripts/profile_voice.py --repeats $(or $(REPEATS),3)

agent-e2e:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python scripts/check_agent.py --browser --memory --voice

agent-managed:
	doppler run -- uv run python scripts/check_managed.py --browser

eval-setup:
	uv run python scripts/eval_agent.py setup

eval-memory:
	doppler run -- uv run python scripts/eval_agent.py longmemeval --limit $(or $(LIMIT),3) --grade

eval-tau:
	doppler run -- uv run python scripts/eval_agent.py tau --task-id $(or TASK,18)

eval-tau-audit:
	uv run python scripts/eval_agent.py tau-audit
