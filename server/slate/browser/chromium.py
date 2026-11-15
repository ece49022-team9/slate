import asyncio
import json
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from aiohttp import ClientSession, web

from slate.browser.actions import Recorder
from slate.browser.proxy import CHROME, Proxy
from slate.browser.watch import Watch

logger = logging.getLogger("slate.browser.machine")
DISPLAY = ":1"
WIDTH, HEIGHT = 1440, 900
PANEL = 30
PROFILE = Path("/root/slate-browser")
LOGS = Path("/tmp/slate-browser-logs")
READY = Path("/tmp/browser-ready")
VOXL = "/usr/local/lib/voxl/voxl"
POINTER = (
    "--blink-settings=primaryHoverType=2,availableHoverTypes=2,"
    "primaryPointerType=4,availablePointerTypes=4"
)
CHROME_SWITCHES = [
    f"--user-data-dir={PROFILE}",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=" + CHROME.split(":")[1],
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-search-engine-choice-screen",
    "--hide-crash-restore-bubble",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--password-store=basic",
    "--start-maximized",
    "--window-position=0,0",
    f"--window-size={WIDTH},{HEIGHT}",
    "--enable-unsafe-swiftshader",
    "--use-gl=angle",
    "--use-angle=gl",
    "--ignore-gpu-blocklist",
    "--disable-gpu-compositing",
    "--disable-blink-features=AutomationControlled",
    POINTER,
    "--lang=en-US",
    "--accept-lang=en-US,en",
    "about:blank",
]
VOXL_SWITCHES = [
    "--addr=127.0.0.1",
    "--port=6080",
    "--enable-basic-auth=false",
    "--agent-api=false",
    "--file-transfers=none",
    "--microphone-enabled=false",
    "--audio-enabled=false",
    "--clipboard-images=false",
    "--preset=superfast",
    "--crf=20",
    "--paintover-burst=0",
    "--lossless-still-ms=250",
]


async def fixture(_request: web.Request) -> web.Response:
    code = os.environ["SLATE_BROWSER_TEST_CODE"]
    html = (
        "<!doctype html><title>Slate browser test</title>"
        f"<h1>Apply the code</h1><p>The code is {code}</p>"
        '<label>Code <input id="code"></label>'
        '<button id="apply" onclick="document.getElementById(\'result\').textContent='
        'document.getElementById(\'code\').value">Apply</button><p id="result"></p>'
    )
    return web.Response(text=html, content_type="text/html")


async def shop(_request: web.Request) -> web.Response:
    html = (
        "<!doctype html><title>Slate test shop</title>"
        "<h1>Desk lamp</h1><p>Price: $49.00</p>"
        '<button id="order" onclick="document.getElementById(\'result\').textContent='
        '\'ordered\'">Place order</button><p id="result">not ordered</p>'
    )
    return web.Response(text=html, content_type="text/html")


def ready_profile() -> None:
    default = PROFILE / "Default"
    default.mkdir(parents=True, exist_ok=True)
    for lock in PROFILE.glob("Singleton*"):
        lock.unlink(missing_ok=True)
    for sessions in ("Sessions", "Sessions_Encrypted"):
        shutil.rmtree(default / sessions, ignore_errors=True)
    preferences = default / "Preferences"
    try:
        settings = json.loads(preferences.read_text()) if preferences.exists() else {}
    except json.JSONDecodeError:
        logger.warning(
            "slate.browser.machine: unreadable Chrome preferences; leaving them"
        )
        return
    settings.setdefault("browser", {}).setdefault("theme", {})["color_scheme2"] = 1
    settings.setdefault("extensions", {}).setdefault("theme", {})["system_theme"] = 0
    preferences.write_text(json.dumps(settings))


async def keep_running(
    name: str, command: list[str], before: Callable[[], None] | None = None
) -> None:
    log = LOGS / f"{name}.log"
    while True:
        if before:
            before()
        with log.open("ab") as output:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                env={**os.environ, "DISPLAY": DISPLAY},
            )
        try:
            code = await process.wait()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.terminate()
                await process.wait()
            raise
        tail = log.read_text(errors="replace").splitlines()[-5:]
        logger.warning(
            "slate.browser.machine: %s exited with %s; restarting. %s",
            name,
            code,
            " | ".join(tail),
        )
        await asyncio.sleep(1)


async def run(command: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "DISPLAY": DISPLAY},
    )
    output, _ = await process.communicate()
    return output.decode(errors="replace")


async def wait_for(what: str, check: Callable, seconds: float = 30) -> None:
    async with asyncio.timeout(seconds):
        while not await check():
            await asyncio.sleep(0.2)
    logger.info("slate.browser.machine: %s is up", what)


async def display_up() -> bool:
    return Path("/tmp/.X11-unix/X" + DISPLAY[1:]).exists()


async def window_manager_up() -> bool:
    return "window id" in await run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"])


async def answers(url: str) -> bool:
    try:
        async with ClientSession() as session, session.get(url) as response:
            return response.status == 200
    except OSError:
        return False


async def leave_panel() -> None:
    area = f"0, 0, {WIDTH}, {HEIGHT - PANEL}"
    await run(
        ["xprop", "-root", "-f", "_NET_WORKAREA", "32c", "-set", "_NET_WORKAREA", area]
    )


async def serve(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    LOGS.mkdir(parents=True, exist_ok=True)
    recorder = Recorder()
    fixtures = web.Application()
    fixtures.router.add_get("/fixture", fixture)
    fixtures.router.add_get("/shop", shop)
    runners = [await serve(Proxy(recorder).app(), 9222), await serve(fixtures, 8080)]
    screen = f"{WIDTH}x{HEIGHT}x24"
    async with asyncio.TaskGroup() as tasks:
        display = [
            "Xvfb",
            DISPLAY,
            "-screen",
            "0",
            screen,
            "-shmem",
            "-nolisten",
            "tcp",
        ]
        tasks.create_task(keep_running("display", display))
        await wait_for("the display", display_up)
        tasks.create_task(keep_running("window-manager", ["openbox"]))
        await wait_for("the window manager", window_manager_up)
        await leave_panel()
        tasks.create_task(
            keep_running(
                "chrome", ["google-chrome-stable", *CHROME_SWITCHES], ready_profile
            )
        )
        await wait_for("Chrome", lambda: answers(f"http://{CHROME}/json/version"))
        tasks.create_task(keep_running("voxl", [VOXL, *VOXL_SWITCHES]))
        await wait_for("voxl", lambda: answers("http://127.0.0.1:6080/healthz"))
        tasks.create_task(Watch(recorder).run())
        READY.touch()
        logger.info("slate.browser.machine: ready")
    for runner in runners:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
