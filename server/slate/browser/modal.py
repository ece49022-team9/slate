import argparse
import asyncio
import json
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

import httpx
import modal
from modal.exception import NotFoundError, SandboxTerminatedError

from slate.agent.runtime import BROWSER as STATE
from slate.agent.runtime import point_browser

logger = logging.getLogger("slate.browser")
IMAGE = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install("playwright==1.62.0", "aiohttp==3.13.3")
    .run_commands("playwright install --with-deps chromium")
    .add_local_file(Path(__file__).with_name("chromium.py"), "/browser.py")
)


async def start() -> None:
    os.umask(0o077)
    if STATE.exists():
        raise RuntimeError("Browser state already exists; run make browser-stop first")
    app = await modal.App.lookup.aio("slate-browser", create_if_missing=True)
    code = secrets.token_hex(8)
    sandbox = await modal.Sandbox.create.aio(
        "python",
        "/browser.py",
        app=app,
        image=IMAGE,
        timeout=4 * 3600,
        experimental_options={"vm_runtime": True},
        readiness_probe=modal.Probe.with_exec("test", "-f", "/tmp/browser-ready"),
        env={"SLATE_BROWSER_TEST_CODE": code},
    )
    try:
        await sandbox.wait_until_ready.aio(timeout=60)
        process = await sandbox.exec.aio(
            "python",
            "-c",
            "import urllib.request; "
            "print(urllib.request.urlopen('http://127.0.0.1:9222/json/version')"
            ".read().decode())",
        )
        version = json.loads(await process.stdout.read.aio())
        credentials = await sandbox.create_connect_token.aio(port=9222)
        fixture_credentials = await sandbox.create_connect_token.aio(port=8080)
        path = urlparse(version["webSocketDebuggerUrl"]).path
        cdp = credentials.url.replace("https://", "wss://").rstrip("/") + path
        cdp += "?_modal_connect_token=" + credentials.token
        async with httpx.AsyncClient() as client:
            response = await client.get(credentials.url + "/json/version")
            if response.status_code not in (401, 403):
                raise RuntimeError("Modal allowed unauthenticated access to Chromium")
            response = await client.get(
                credentials.url + "/json/version",
                headers={"Authorization": "Bearer " + credentials.token},
            )
            response.raise_for_status()
            if "webSocketDebuggerUrl" not in response.json():
                raise RuntimeError("Authenticated Modal endpoint did not expose CDP")
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(
            json.dumps(
                {
                    "sandbox_id": sandbox.object_id,
                    "cdp_url": cdp,
                    "test_code": code,
                    "fixture_url": fixture_credentials.url.rstrip("/")
                    + "/fixture"
                    + "?_modal_connect_token="
                    + fixture_credentials.token,
                }
            )
        )
        point_browser()
        print(
            f"slate.browser: authenticated Modal Chromium ready ({sandbox.object_id}); "
            "run make browser-stop when done"
        )
    except BaseException:
        await sandbox.terminate.aio()
        raise


async def stop() -> None:
    if STATE.exists():
        try:
            sandbox = await modal.Sandbox.from_id.aio(
                json.loads(STATE.read_text())["sandbox_id"]
            )
            await sandbox.terminate.aio()
        except (NotFoundError, SandboxTerminatedError):
            print("slate.browser: sandbox already stopped; clearing local state")
        STATE.unlink()
        point_browser()
        print("slate.browser: Modal Chromium stopped")


async def ensure() -> None:
    if not STATE.exists():
        return
    try:
        sandbox = await modal.Sandbox.from_id.aio(
            json.loads(STATE.read_text())["sandbox_id"]
        )
        if await sandbox.poll.aio() is None:
            return
    except NotFoundError:
        pass
    logger.warning("slate.browser: Modal Chromium stopped; starting a new one")
    STATE.unlink()
    await start()


async def run() -> None:
    logging.basicConfig(level=logging.INFO)
    if not STATE.exists():
        await start()
    try:
        while True:
            try:
                await ensure()
            except Exception:
                logger.exception("slate.browser: could not restart Modal Chromium")
            await asyncio.sleep(60)
    finally:
        await stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "start", "stop"])
    args = parser.parse_args()
    commands = {"run": run, "start": start, "stop": stop}
    asyncio.run(commands[args.command]())


if __name__ == "__main__":
    main()
