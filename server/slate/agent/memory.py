import os
import uuid
from datetime import datetime, timezone
from typing import Protocol

import turbopuffer


class Memory(Protocol):
    def get_context(self, query: str | None = None) -> list[dict]:
        ...

    def add_message(self, message: dict) -> None:
        ...


class InMemoryMemory:
    def __init__(self, max_messages: int = 20):
        self.messages: list[dict] = []
        self.max_messages = max_messages

    def get_context(self, query: str | None = None) -> list[dict]:
        return list(self.messages[-self.max_messages :])

    def add_message(self, message: dict) -> None:
        self.messages.append(message)


class TurbopufferMemory:
    def __init__(
        self,
        user_id: str = "default",
        namespace: str | None = None,
        region: str = "gcp-us-central1",
        recent_messages: int = 10,
        semantic_results: int = 5,
    ):
        api_key = os.getenv("TURBOPUFFER_API_KEY")

        if not api_key:
            raise ValueError(
                "TURBOPUFFER_API_KEY is not set"
            )

        self.user_id = user_id
        self.recent_messages = recent_messages
        self.semantic_results = semantic_results

        self.messages: list[dict] = []

        self.client = turbopuffer.Turbopuffer(
            api_key=api_key,
            region=region,
        )

        self.namespace = self.client.namespace(
            namespace or os.getenv(
                "TURBOPUFFER_NAMESPACE",
                "slate-memory",
            )
        )

    def get_context(
        self,
        query: str | None = None,
    ) -> list[dict]:

        recent = self.messages[-self.recent_messages :]

        if not query:
            return list(recent)

        try:
            result = self.namespace.query(
                filters=(
                    "user_id",
                    "Eq",
                    self.user_id,
                ),
                rank_by=(
                    "text",
                    "ANN",
                    (
                        "Embed",
                        query,
                    ),
                ),
                limit=self.semantic_results,
                include_attributes=[
                    "text",
                    "created_at",
                ],
            )

            semantic_messages = []

            for row in result.rows:
                text = getattr(row, "text", None)

                if not text:
                    continue

                semantic_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Relevant memory from a previous "
                            f"conversation:\n{text}"
                        ),
                    }
                )

            return semantic_messages + list(recent)

        except Exception as e:
            print(f"[Memory] Turbopuffer query failed: {e}")

            # If the semantic store is temporarily unavailable,
            # Slate should still be able to operate using recent context.
            return list(recent)

    def add_message(self, message: dict) -> None:
        self.messages.append(message)

        role = message.get("role")
        content = message.get("content")

        if not content or role not in {"user", "assistant"}:
            return

        try:
            self.namespace.write(
                upsert_rows=[
                    {
                        "id": str(uuid.uuid4()),
                        "user_id": self.user_id,
                        "role": role,
                        "text": str(content),
                        "created_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }
                ],
                distance_metric="cosine_distance",
                schema={
                    "text": {
                        "type": "string",
                        "embed": {
                            "model": "nvidia/nemotron-3-embed-8b",
                            "dims": 1024,
                        },
                    },
                    "user_id": {
                        "type": "string",
                    },
                    "role": {
                        "type": "string",
                    },
                    "created_at": {
                        "type": "string",
                    },
                },
            )

        except Exception as e:
            print(f"[Memory] Turbopuffer write failed: {e}")