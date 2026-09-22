import asyncio

from slate.agent.agent import Agent


async def main():
    agent = Agent()

    try:
        await agent.initialize()
        print("MCP initialization finished")

    finally:
        await agent.close()
        print("MCP connections closed")


asyncio.run(main())