import json

from slate.agent.mcp_client import MCPManager
from slate.agent.model import Model
from slate.agent.tools import (
    TOOL_DEFINITIONS,
    TOOLS,
)


class Agent:
    def __init__(self):
        self.model = Model("openrouter/free")

        self.mcp = MCPManager()

    async def initialize(self):
        await self._connect_mcp_servers()

    async def _connect_mcp_servers(self):
        servers = {
            "gmail": ("https://gmailmcp.googleapis.com/mcp/v1"),
            "calendar": ("https://calendarmcp.googleapis.com/mcp/v1"),
        }
        for name, url in servers.items():
            try:
                await self.mcp.connect_oauth(
                    name,
                    url,
                )
            except Exception as e:
                print(f"[MCP] Could not connect to {name}: {e}")

    async def run(
        self,
        message: str,
    ) -> str:

        mcp_tools = self.mcp.get_tool_definitions()

        all_tools = TOOL_DEFINITIONS + mcp_tools

        messages = [
            {
                "role": "user",
                "content": message,
            }
        ]

        while True:
            response = self.model.chat(
                messages,
                all_tools,
            )

            choice = response.choices[0]

            assistant_message = choice.message

            if not assistant_message.tool_calls:
                return assistant_message.content or ""

            messages.append(assistant_message)

            for tool_call in assistant_message.tool_calls:
                tool_name = tool_call.function.name

                arguments = json.loads(tool_call.function.arguments or "{}")

                if tool_name in TOOLS:
                    result = TOOLS[tool_name](arguments)

                elif tool_name in self.mcp.tools:
                    result = await self.mcp.call_tool(
                        tool_name,
                        arguments,
                    )

                else:
                    result = f"Unknown tool: {tool_name}"

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": (tool_call.id),
                        "content": str(result),
                    }
                )

    async def close(self):
        await self.mcp.close()
