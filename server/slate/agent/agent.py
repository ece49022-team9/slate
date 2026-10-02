import asyncio

from slate.agent.model import Model


class Agent:
    def __init__(self, model: Model | None = None) -> None:
        self.model = model or Model()
        self.messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    "You are Slate, a voice assistant. Answer the user's spoken "
                    "request briefly in natural spoken language. This voice test "
                    "has no browser, "
                    "account, or device-action tools; do not claim you used them."
                ),
            }
        ]

    async def run(self, message: str) -> str:
        messages = [*self.messages, {"role": "user", "content": message}]
        response = await asyncio.to_thread(self.model.chat, messages)
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("Agent returned no spoken reply")
        self.messages = [
            self.messages[0],
            *messages[1:][-18:],
            {"role": "assistant", "content": text},
        ]
        return text
