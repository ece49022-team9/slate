import json
import os
import webbrowser
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from time import sleep, time
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.auth import (
    AuthorizationCodeResult,
    OAuthClientProvider,
    TokenStorage,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)


class FileTokenStorage(TokenStorage):
    def __init__(self, path: str):
        self.path = path
        self.tokens = None
        self.client_info = None

        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path) as f:
                data = json.load(f)

            if data.get("tokens"):
                self.tokens = OAuthToken.model_validate(data["tokens"])

            if data.get("client_info"):
                self.client_info = OAuthClientInformationFull.model_validate(
                    data["client_info"]
                )

        except Exception:
            self.tokens = None
            self.client_info = None

    def _save(self):
        os.makedirs(
            os.path.dirname(self.path),
            exist_ok=True,
        )

        data = {
            "tokens": (self.tokens.model_dump() if self.tokens else None),
            "client_info": (
                self.client_info.model_dump() if self.client_info else None
            ),
        }

        with open(self.path, "w") as f:
            json.dump(
                data,
                f,
                indent=2,
                default=str,
            )

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens
        self._save()

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info
        self._save()


class OAuthCallbackServer:
    def __init__(self, port: int = 3030):
        self.port = port
        self.data = {
            "code": None,
            "state": None,
            "iss": None,
            "error": None,
        }

        self.server = None
        self.thread = None

    def start(self):
        callback_data = self.data

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)

                if "code" in params:
                    callback_data["code"] = params["code"][0]

                    callback_data["state"] = params.get(
                        "state",
                        [None],
                    )[0]

                    callback_data["iss"] = params.get(
                        "iss",
                        [None],
                    )[0]

                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        "text/html",
                    )
                    self.end_headers()

                    self.wfile.write(
                        b"""
                        <html>
                        <body>
                        <h2>Slate authorization successful.</h2>
                        <p>You can close this window.</p>
                        </body>
                        </html>
                        """
                    )

                elif "error" in params:
                    callback_data["error"] = params["error"][0]

                    self.send_response(400)
                    self.end_headers()

                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                return

        self.server = HTTPServer(
            ("localhost", self.port),
            Handler,
        )

        self.thread = Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

        self.thread.start()

    def wait(self, timeout=300):
        start = time()

        while time() - start < timeout:
            if self.data["error"]:
                raise RuntimeError(f"OAuth error: {self.data['error']}")

            if self.data["code"]:
                return AuthorizationCodeResult(
                    code=self.data["code"],
                    state=self.data["state"],
                    iss=self.data["iss"],
                )

            sleep(0.1)

        raise TimeoutError("Timed out waiting for OAuth callback.")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

        if self.thread:
            self.thread.join(timeout=1)


class MCPManager:
    def __init__(self):
        self.clients = {}
        self.tools = {}
        self.exit_stack = AsyncExitStack()

    async def connect_stdio(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ):
        server = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        client = Client(server)

        await self.exit_stack.enter_async_context(client)

        self.clients[name] = client

        await self._register_tools(
            name,
            client,
        )

    async def connect_http(
        self,
        name: str,
        url: str,
    ):
        client = Client(url)

        await self.exit_stack.enter_async_context(client)

        self.clients[name] = client

        await self._register_tools(
            name,
            client,
        )

    async def connect_oauth(
        self,
        name: str,
        url: str,
    ):
        token_path = os.path.join(
            ".slate",
            "oauth",
            f"{name}.json",
        )

        storage = FileTokenStorage(token_path)

        callback_server = OAuthCallbackServer(port=3030)

        callback_server.start()

        async def redirect_handler(
            authorization_url: str,
        ):
            print()
            print(f"[MCP] Authorizing {name}...")
            print("[MCP] Opening browser...")

            webbrowser.open(authorization_url)

        async def callback_handler():
            print("[MCP] Waiting for OAuth callback...")

            try:
                return callback_server.wait()
            finally:
                callback_server.stop()

        metadata = OAuthClientMetadata(
            client_name="Slate",
            redirect_uris=["http://localhost:3030/callback"],
            grant_types=[
                "authorization_code",
                "refresh_token",
            ],
            response_types=["code"],
        )

        oauth = OAuthClientProvider(
            server_url=url,
            client_metadata=metadata,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

        custom_client = httpx2.AsyncClient(auth=oauth)

        try:
            from mcp.client.streamable_http import (
                streamable_http_client,
            )

            transport = await self.exit_stack.enter_async_context(
                streamable_http_client(
                    url,
                    http_client=custom_client,
                )
            )

            read_stream, write_stream = transport

            from mcp import ClientSession

            session = ClientSession(
                read_stream,
                write_stream,
            )

            await self.exit_stack.enter_async_context(session)

            await session.initialize()

            self.clients[name] = session

            await self._register_tools(
                name,
                session,
            )

        except Exception:
            await custom_client.aclose()
            raise

    async def _register_tools(
        self,
        name: str,
        client,
    ):
        result = await client.list_tools()

        for tool in result.tools:
            exposed_name = f"{name}_{tool.name}"

            self.tools[exposed_name] = {
                "server": name,
                "name": tool.name,
                "tool": tool,
            }

        print(f"[MCP] {name}: {len(result.tools)} tools available")

    def get_tool_definitions(self):
        definitions = []

        for exposed_name, info in self.tools.items():
            tool = info["tool"]

            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": exposed_name,
                        "description": (tool.description or ""),
                        "parameters": (tool.inputSchema),
                    },
                }
            )

        return definitions

    async def call_tool(
        self,
        exposed_name: str,
        arguments: dict,
    ):
        if exposed_name not in self.tools:
            raise ValueError(f"MCP tool not found: {exposed_name}")

        info = self.tools[exposed_name]

        client = self.clients[info["server"]]

        result = await client.call_tool(
            info["name"],
            arguments=arguments,
        )

        return self._format_result(result)

    def _format_result(self, result):
        output = []

        if result.content:
            for item in result.content:
                if hasattr(item, "text"):
                    output.append(item.text)
                else:
                    output.append(str(item))

        if result.structuredContent:
            output.append(
                json.dumps(
                    result.structuredContent,
                    default=str,
                )
            )

        return "\n".join(output)

    async def close(self):
        await self.exit_stack.aclose()
