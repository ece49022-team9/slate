import os

from slate.agent.agent import Agent
from slate.agent.managed import ManagedAgent


def create_agent() -> Agent | ManagedAgent:
    harness = os.getenv("SLATE_HARNESS", "hermes")
    if harness == "hermes":
        return Agent()
    if harness == "openai":
        return ManagedAgent()
    raise ValueError(f"Unknown SLATE_HARNESS: {harness}")
