import asyncio
import os
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web
from playwright.async_api import async_playwright


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


async def proxy(request: web.Request) -> web.StreamResponse:
    url = "http://127.0.0.1:9223" + request.path
    async with ClientSession() as client:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            async with client.ws_connect(url) as upstream:
                downstream = web.WebSocketResponse()
                await downstream.prepare(request)

                async def relay(source, destination) -> None:
                    async for message in source:
                        if message.type == WSMsgType.TEXT:
                            await destination.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            await destination.send_bytes(message.data)
                    await destination.close()

                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(relay(upstream, downstream))
                    tasks.create_task(relay(downstream, upstream))
                return downstream
        async with client.get(url) as response:
            return web.Response(
                body=await response.read(),
                status=response.status,
                content_type=response.content_type,
            )


async def main() -> None:
    async with async_playwright() as playwright:
        process = await asyncio.create_subprocess_exec(
            playwright.chromium.executable_path,
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--remote-debugging-port=9223",
            "--remote-allow-origins=*",
            "--user-data-dir=/tmp/slate-browser",
            "about:blank",
        )
        app = web.Application()
        app.router.add_route("GET", "/{path:.*}", proxy)
        runner = web.AppRunner(app)
        fixture_app = web.Application()
        fixture_app.router.add_get("/fixture", fixture)
        fixture_app.router.add_get("/shop", shop)
        fixture_runner = web.AppRunner(fixture_app)
        try:
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", 9222).start()
            await fixture_runner.setup()
            await web.TCPSite(fixture_runner, "0.0.0.0", 8080).start()
            async with asyncio.timeout(30), ClientSession() as client:
                while True:
                    if process.returncode is not None:
                        raise RuntimeError("Chromium exited before it was ready")
                    try:
                        async with client.get(
                            "http://127.0.0.1:9223/json/version"
                        ) as response:
                            response.raise_for_status()
                            Path("/tmp/browser-ready").touch()
                        break
                    except OSError:
                        await asyncio.sleep(0.2)
            await process.wait()
        finally:
            await fixture_runner.cleanup()
            await runner.cleanup()
            if process.returncode is None:
                process.terminate()
                await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
