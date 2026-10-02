import os

from openai import OpenAI


class Model:
    def __init__(self, model: str | None = None) -> None:
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=key,
            timeout=30,
            max_retries=0,
        )
        self.name = model or os.getenv("SLATE_MODEL", "openrouter/free")

    def chat(self, messages: list[dict]):
        return self.client.chat.completions.create(model=self.name, messages=messages)
