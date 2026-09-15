#basic Agent layer above the model
from slate.agent.model import Model
from slate.agent.tools import get_current_time


class Agent:
    def __init__(self):
        self.model = Model("openrouter/free")

    def run(self, message: str) -> str:
        messages = [
            {"role": "user", "content": message},
        ]

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "description": "Get the current local date and time.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
        ]

        response = self.model.chat(messages, tools)
        choice = response.choices[0]

        if choice.message.tool_calls:
            tool_call = choice.message.tool_calls[0]

            if tool_call.function.name == "get_current_time":
                result = get_current_time()

                messages.append(choice.message)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )

                response = self.model.chat(messages, tools)

        return response.choices[0].message.content or ""