import argparse
import json
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from pathlib import Path

import modal

from slate.agent import runtime
from slate.board import ROOT

logger = logging.getLogger("slate.cloud")
HERMES = tomllib.loads((ROOT / "experiments/agent.toml").read_text())["hermes"]
SOURCE = "/opt/hermes"
DATA = "/data"
HOME = "/root/hermes-home"
SAVED = Path(DATA) / "hermes"
SAVE_SECONDS = 300
KEPT_FILES = ("auth.json", "SOUL.md")
KEPT_DIRS = ("memories", "sessions", "skills", "cron", "kanban", "pairing", "hooks")
KEPT_DATABASES = ("state.db", "kanban.db", "response_store.db", "shared-state.db")
LOCAL = ROOT / ".local/cloud.json"
APP = "slate"
SECRET = "slate-cloud"

app = modal.App(APP, include_source=False)
state = modal.Volume.from_name("slate-hermes", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "build-essential",
        "ca-certificates",
        "curl",
        "git",
        "libffi-dev",
        "procps",
        "ripgrep",
    )
    .pip_install(f"uv=={HERMES['uv_version']}")
    .add_local_file(runtime.ARCHIVE, "/tmp/hermes.tgz", copy=True)
    .run_commands(
        f"echo '{HERMES['source_sha256']}  /tmp/hermes.tgz' | sha256sum -c",
        f"mkdir -p {SOURCE}",
        f"tar -xzf /tmp/hermes.tgz -C {SOURCE} --strip-components=1",
        f"cd {SOURCE} && uv sync --frozen --python {HERMES['python']} --no-dev "
        "--extra messaging --extra mcp",
        f"cd {SOURCE} && HERMES_HOME={HOME} uv run --no-sync python -c "
        "\"import pm; pm.sync_venv(['messaging', 'mcp', 'modal'], explicit=True)\"",
        f"HERMES_HOME={HOME} {SOURCE}/.venv/bin/hermes pm install agent-browser",
    )
    .uv_sync(str(ROOT), extra_options="--python 3.12")
    .env(
        {
            "PYTHONPATH": "/opt/slate/server",
            "SLATE_HERMES_HOME": HOME,
            "HERMES_ALLOW_ROOT_GATEWAY": "1",
            "SLATE_DEVICE_URL": "http://127.0.0.1:8000",
            "SLATE_DEVICE_MODE": os.environ.get("SLATE_DEVICE_MODE", "monty"),
        }
    )
    .add_local_dir(ROOT / "server/slate", "/opt/slate/server/slate")
    .add_local_dir(ROOT / "hermes", "/opt/slate/hermes")
    .add_local_file(
        ROOT / "experiments/agent.toml", "/opt/slate/experiments/agent.toml"
    )
)


def restore() -> None:
    for name in (*KEPT_FILES, *KEPT_DATABASES):
        if (SAVED / name).exists():
            shutil.copyfile(SAVED / name, runtime.PROFILE / name)
    for name in KEPT_DIRS:
        if (SAVED / name).exists():
            shutil.copytree(SAVED / name, runtime.PROFILE / name, dirs_exist_ok=True)
    logger.info("slate.cloud: restored Hermes state from the volume")


def save() -> None:
    """Hermes keeps SQLite databases and lock files, which need a local disk, so
    its home lives in the container and only its lasting state is copied to the
    volume."""
    SAVED.mkdir(parents=True, exist_ok=True)
    for name in KEPT_FILES:
        if (runtime.PROFILE / name).exists():
            shutil.copyfile(runtime.PROFILE / name, SAVED / name)
    for name in KEPT_DIRS:
        if (runtime.PROFILE / name).exists():
            shutil.copytree(runtime.PROFILE / name, SAVED / name, dirs_exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        for name in KEPT_DATABASES:
            if not (runtime.PROFILE / name).exists():
                continue
            copy = Path(scratch) / name
            with (
                sqlite3.connect(runtime.PROFILE / name) as source,
                sqlite3.connect(copy) as target,
            ):
                source.backup(target)
            shutil.copyfile(copy, SAVED / name)
    state.commit()
    logger.info("slate.cloud: saved Hermes state to the volume")


def keep_saving(stopping: threading.Event) -> None:
    while not stopping.wait(SAVE_SECONDS):
        try:
            save()
        except Exception:
            logger.exception("slate.cloud: could not save Hermes state")


def wait_for_port(port: int, process: subprocess.Popen, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"slate.cloud: process on port {port} exited early")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"slate.cloud: nothing answered on port {port}")


@app.cls(
    image=image,
    volumes={DATA: state},
    secrets=[modal.Secret.from_name(SECRET)],
    min_containers=1,
    max_containers=1,
    timeout=24 * 3600,
    cpu=2,
    memory=4096,
)
@modal.concurrent(max_inputs=200)
class Slate:
    @modal.enter()
    def start(self) -> None:
        """Hermes runs as an ordinary Modal client with its own venv, so it gets
        neither this container's Python path nor its container identity."""
        logging.basicConfig(level=logging.INFO)
        runtime.PROFILE.mkdir(parents=True, exist_ok=True)
        restore()
        if not (runtime.PROFILE / "auth.json").exists():
            raise RuntimeError("slate.cloud: no Hermes login; run make cloud-setup")
        runtime.write_profile(os.environ["SLATE_AGENT_KEY"], host="0.0.0.0")
        hermes = [f"{SOURCE}/.venv/bin/hermes"]
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in ("PYTHONPATH", "OPENAI_API_KEY")
            and (not name.startswith("MODAL_") or name.startswith("MODAL_TOKEN_"))
        }
        env["HERMES_HOME"] = str(runtime.PROFILE)
        self.processes = [
            subprocess.Popen([*hermes, "gateway", "run", "--replace"], env=env),
            subprocess.Popen([sys.executable, "-m", "slate.browser.modal", "run"]),
        ]
        wait_for_port(8642, self.processes[0], 120)
        logger.info("slate.cloud: Hermes gateway ready")
        self.stopping = threading.Event()
        self.saver = threading.Thread(target=keep_saving, args=(self.stopping,))
        self.saver.start()

    @modal.web_server(8000, startup_timeout=60)
    def serve(self) -> None:
        self.processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "slate.app:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                    "--ws-ping-interval",
                    "20",
                ]
            )
        )

    @modal.web_server(8642, startup_timeout=60)
    def agent(self) -> None:
        logger.info("slate.cloud: exposing the Hermes API behind its key")

    @modal.exit()
    def stop(self) -> None:
        self.stopping.set()
        self.saver.join()
        for process in reversed(self.processes):
            process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                logger.error("slate.cloud: pid %s ignored SIGTERM", process.pid)
                process.kill()
        save()


def local_settings() -> dict:
    return json.loads(LOCAL.read_text()) if LOCAL.exists() else {}


def setup() -> None:
    os.umask(0o077)
    settings = local_settings()
    settings.setdefault("device_token", secrets.token_urlsafe(32))
    settings.setdefault("agent_key", secrets.token_urlsafe(32))
    LOCAL.parent.mkdir(parents=True, exist_ok=True)
    LOCAL.write_text(json.dumps(settings, indent=2))
    profile = os.environ["MODAL_PROFILE"]
    modal_login = tomllib.loads((Path.home() / ".modal.toml").read_text())[profile]
    modal.Secret.objects.delete(SECRET, allow_missing=True)
    modal.Secret.objects.create(
        SECRET,
        {
            "SLATE_DEVICE_TOKEN": settings["device_token"],
            "SLATE_AGENT_KEY": settings["agent_key"],
            "MODAL_TOKEN_ID": modal_login["token_id"],
            "MODAL_TOKEN_SECRET": modal_login["token_secret"],
            "SLATE_PUBLIC_URL": settings.get("url", ""),
            "OPENAI_API_KEY": os.environ["OPENAI_API_KEY"],
        },
    )
    saved = {entry.path for entry in state.listdir("/", recursive=True)}
    if "hermes/auth.json" in saved:
        print(f"slate.cloud: secret {SECRET} updated; Hermes login already in Modal")
        return
    login = runtime.PROFILE / "auth.json"
    if not login.exists():
        raise RuntimeError(f"slate.cloud: {login} is missing; run make agent-login")
    with state.batch_upload() as upload:
        upload.put_file(login, "/hermes/auth.json")
    print(f"slate.cloud: secret {SECRET} and Hermes login uploaded")


def deploy() -> None:
    command = [shutil.which("modal") or "modal", "deploy", "-m", "slate.cloud"]
    subprocess.run(command, check=True)
    settings = local_settings()
    service = modal.Cls.from_name(APP, "Slate")()
    url = service.serve.get_web_url()
    settings["agent_url"] = service.agent.get_web_url()
    if settings.get("url") != url:
        settings["url"] = url
        LOCAL.write_text(json.dumps(settings, indent=2))
        print("slate.cloud: publishing the service URL to the container secret")
        setup()
        subprocess.run(command, check=True)
    LOCAL.write_text(json.dumps(settings, indent=2))
    print(f"slate.cloud: deployed at {url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["setup", "deploy"])
    {"setup": setup, "deploy": deploy}[parser.parse_args().command]()


if __name__ == "__main__":
    main()
