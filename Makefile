.PHONY: setup server web check firmware firmware-setup firmware-sim firmware-check

setup:
	uv sync --locked
	npm --prefix web ci

server:
	uv run uvicorn slate.app:app --reload --host 127.0.0.1 --port 8000

web:
	npm --prefix web run dev

check:
	uv run ruff check server
	uv run ruff format --check server
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
