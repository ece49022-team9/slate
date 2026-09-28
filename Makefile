.PHONY: setup server web check firmware firmware-setup firmware-sim firmware-check firmware-hardware firmware-flash firmware-monitor firmware-ports firmware-mic-check firmware-display firmware-display-check voice-deploy voice-check livekit

MODAL_PROFILE ?= sudarshan-1

setup:
	uv sync --locked
	npm --prefix web ci

server:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run uvicorn slate.app:app --reload --host 127.0.0.1 --port 8000

web:
	npm --prefix web run dev

check:
	uv run ruff check server scripts tests
	uv run ruff format --check server scripts tests
	uv run python -m unittest discover -s tests
	npm --prefix web run lint
	npm --prefix web run build

firmware:
	bash scripts/esp-idf.sh build

firmware-setup:
	bash scripts/esp-idf.sh setup

firmware-sim:
	bash scripts/esp-idf.sh qemu

firmware-check:
	uv run python scripts/check_firmware.py

voice-deploy:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run modal deploy -m slate.voice.stt
	MODAL_PROFILE=$(MODAL_PROFILE) uv run modal deploy -m slate.voice.tts

voice-check:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run python -m slate.voice check

livekit:
	livekit-server --dev --bind 127.0.0.1 --node-ip 127.0.0.1

firmware-hardware:
	uv tool run --from platformio==6.2.0 pio run -d firmware

firmware-ports:
	uv tool run --from platformio==6.2.0 pio device list

firmware-flash:
	@test -n "$(PORT)" || (printf 'Set PORT to the dev board serial port, for example: make firmware-flash PORT=/dev/cu.usbserial-0001\n' >&2; exit 1)
	uv tool run --from platformio==6.2.0 pio run -d firmware -t upload --upload-port "$(PORT)"

firmware-monitor:
	@test -n "$(PORT)" || (printf 'Set PORT to the dev board serial port, for example: make firmware-monitor PORT=/dev/cu.usbserial-0001\n' >&2; exit 1)
	uv tool run --from platformio==6.2.0 pio device monitor -d firmware -p "$(PORT)" -b 115200

firmware-mic-check:
	uv run python scripts/check_mic.py

firmware-display:
	uv run python -m slate.display

firmware-display-check:
	uv run python scripts/check_display.py
