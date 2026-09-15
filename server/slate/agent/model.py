import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# using /free right now, can customize specific free models
#50 requests per day for free acounts
class Model:
    def __init__(self, model: str):
        api_key = os.getenv("OPENROUTER_API_KEY")

        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")

        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )
        self.model = model

    def chat(self, messages: list, tools: list | None = None):
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
        )