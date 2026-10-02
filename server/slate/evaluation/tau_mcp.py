import argparse
import asyncio
import json
from pathlib import Path

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server


async def serve(metadata: Path, url: str) -> None:
    tools = json.loads(metadata.read_text())["tools"]
    async with httpx.AsyncClient(base_url=url, timeout=300) as client:

        async def list_tools(context, params):
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name=tool["name"],
                        description=tool["description"],
                        inputSchema=tool["parameters"],
                    )
                    for tool in tools
                ]
            )

        async def call_tool(context, params):
            response = await client.post(
                "/step", json={"tool": params.name, "arguments": params.arguments or {}}
            )
            response.raise_for_status()
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=response.json()["observation"])
                ]
            )

        server = Server("tau-airline", on_list_tools=list_tools, on_call_tool=call_tool)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.metadata, args.url))
