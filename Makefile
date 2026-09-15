.PHONY: setup server web check firmware

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
	cd firmware && idf.py build
