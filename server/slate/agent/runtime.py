import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path
from urllib.request import urlopen

from slate.board import ROOT

SOURCE = ROOT / ".local/hermes"
PROFILE = ROOT / ".local/hermes-home"
CONFIG = ROOT / "experiments/agent.toml"
GUARD = ROOT / "hermes/slate-guard"
TOOLSETS = [
    "memory",
    "session_search",
    "browser",
    "web",
    "vision",
    "terminal",
    "file",
    "code_execution",
    "skills",
    "todo",
    "delegation",
    "cronjob",
    "connections",
    "mcp-slate-device",
]


def settings() -> dict:
    cfg = tomllib.loads(CONFIG.read_text())["hermes"]
    cfg["model"] = os.getenv("SLATE_MODEL", cfg["model"])
    cfg["provider"] = os.getenv("SLATE_PROVIDER", cfg["provider"])
    return cfg


def command() -> list[str]:
    return ["uv", "tool", "run", "--from", f"uv=={settings()['uv_version']}", "uv"]


def configure_tools(config: dict) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Slate device MCP requires uv on PATH")
    config.setdefault("platform_toolsets", {})["api_server"] = list(TOOLSETS)
    config.setdefault("terminal", {})["backend"] = "modal"
    config.setdefault("bot_desktop", {})["placement"] = "gateway"
    enabled = config.setdefault("plugins", {}).setdefault("enabled", [])
    if "slate-guard" not in enabled:
        enabled.append("slate-guard")
    server = config.setdefault("mcp_servers", {}).setdefault("slate-device", {})
    server.update(
        {
            "command": str(Path(uv).resolve()),
            "args": [
                "--directory",
                str(ROOT),
                "run",
                "--no-sync",
                "python",
                "-m",
                "slate.agent.device_mcp",
            ],
        }
    )
    server.setdefault("env", {}).update(
        SLATE_DEVICE_MODE=os.getenv("SLATE_DEVICE_MODE", "monty"),
        SLATE_DEVICE_URL=os.getenv("SLATE_DEVICE_URL", "http://127.0.0.1:8000"),
    )
    config.setdefault("tools", {}).setdefault("tool_search", {})["enabled"] = "off"
    if profile := os.getenv("MODAL_PROFILE"):
        server["env"]["MODAL_PROFILE"] = profile


def install_guard() -> None:
    target = PROFILE / "plugins/slate-guard"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(GUARD / "plugin.yaml", target / "plugin.yaml")
    shutil.copyfile(GUARD / "__init__.py", target / "__init__.py")
    shutil.copyfile(ROOT / "server/slate/agent/guard.py", target / "policy.py")


def environment() -> dict[str, str]:
    env = dict(os.environ, HERMES_HOME=str(PROFILE))
    env.pop("VIRTUAL_ENV", None)
    if settings()["provider"] == "openai-codex":
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENROUTER_API_KEY", None)
    browser = ROOT / ".local/browser.json"
    if browser.exists():
        env["BROWSER_CDP_URL"] = json.loads(browser.read_text())["cdp_url"]
    return env


def verify_source() -> None:
    marker = SOURCE / ".slate-revision"
    if marker.exists():
        revision = marker.read_text().strip()
    elif (SOURCE / ".git").exists():
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True
        ).strip()
    else:
        raise RuntimeError(
            "Hermes cache is unverified; remove .local/hermes "
            "and rerun make agent-setup"
        )
    if revision != settings()["revision"]:
        raise RuntimeError(
            "Hermes cache has a different revision; remove .local/hermes "
            "and rerun make agent-setup"
        )


def setup() -> None:
    os.umask(0o077)
    cfg = settings()
    archive = ROOT / ".local/hermes-source.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if (
        not archive.exists()
        or hashlib.sha256(archive.read_bytes()).hexdigest() != cfg["source_sha256"]
    ):
        url = (
            f"https://codeload.github.com/{cfg['repository']}/tar.gz/{cfg['revision']}"
        )
        with urlopen(url, timeout=120) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != cfg["source_sha256"]:
        raise RuntimeError("Hermes source checksum does not match the pinned revision")
    if not SOURCE.exists():
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                parts = Path(member.name).parts
                if len(parts) > 1:
                    member.name = str(Path("hermes", *parts[1:]))
                    source.extract(member, archive.parent, filter="data")
        (SOURCE / ".slate-revision").write_text(cfg["revision"])
    verify_source()
    subprocess.run(
        [
            *command(),
            "sync",
            "--frozen",
            "--python",
            cfg["python"],
            "--no-dev",
            "--extra",
            "messaging",
            "--extra",
            "mcp",
        ],
        cwd=SOURCE,
        check=True,
    )
    PROFILE.mkdir(parents=True, exist_ok=True)
    key = PROFILE / "api-key"
    if not key.exists():
        key.write_text(secrets.token_urlsafe(32))
    config = {
        "model": {"default": cfg["model"], "provider": cfg["provider"]},
        "agent": {"max_turns": "unlimited"},
        "browser": {"backend": "off"},
        "gateway": {
            "api_server": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 8642,
                "key": key.read_text().strip(),
            }
        },
        "auth": {"adopt_external_logins": False},
    }
    configure_tools(config)
    install_guard()
    (PROFILE / "config.yaml").write_text(json.dumps(config, indent=2))
    subprocess.run(
        [*command(), "run", "--no-sync", "hermes", "pm", "install", "agent-browser"],
        cwd=SOURCE,
        env=environment(),
        check=True,
    )
    subprocess.run(
        [
            *command(),
            "run",
            "--no-sync",
            "python",
            "-c",
            "import pm; pm.sync_venv(['modal'], explicit=True)",
        ],
        cwd=SOURCE,
        env=environment(),
        check=True,
    )
    print("slate.agent: pinned Hermes installed; private profile configured")
    if not (PROFILE / "auth.json").exists():
        print("slate.agent: run make agent-login to authorize your subscription")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["setup", "start", "login"])
    args = parser.parse_args()
    if args.command == "setup":
        setup()
        return
    verify_source()
    if args.command == "start":
        config_path = PROFILE / "config.yaml"
        config = json.loads(config_path.read_text())
        configure_tools(config)
        install_guard()
        config_path.write_text(json.dumps(config, indent=2))
    hermes_args = (
        ["gateway", "run", "--replace"]
        if args.command == "start"
        else ["auth", "add", "openai-codex"]
    )
    subprocess.run(
        [*command(), "run", "--no-sync", "hermes", *hermes_args],
        cwd=SOURCE,
        env=environment(),
        check=True,
    )


if __name__ == "__main__":
    main()
