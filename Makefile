.PHONY: setup server web check firmware firmware-setup firmware-sim firmware-check voice-deploy voice-check

MODAL_PROFILE ?= sudarshan-1

setup:
	uv sync --locked
	npm --prefix web ci

server:
	uv run uvicorn slate.app:app --reload --host 127.0.0.1 --port 8000

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
	MODAL_PROFILE=$(MODAL_PROFILE) uv run --group voice modal deploy -m slate.voice.stt
	MODAL_PROFILE=$(MODAL_PROFILE) uv run --group voice modal deploy -m slate.voice.tts

voice-check:
	MODAL_PROFILE=$(MODAL_PROFILE) uv run --group voice python -m slate.voice check
