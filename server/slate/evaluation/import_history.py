import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from hermes_state import SessionDB


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("history", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    data = json.loads(args.history.read_text())
    sessions = []
    for index, (date, messages) in enumerate(
        zip(data["haystack_dates"], data["haystack_sessions"], strict=True)
    ):
        timestamp = (
            datetime.strptime(date, "%Y/%m/%d (%a) %H:%M")
            .replace(tzinfo=UTC)
            .timestamp()
        )
        sessions.append(
            {
                "id": f"history_{index:04}",
                "source": "api_server",
                "title": f"Historical conversation {index:04} at {date}",
                "started_at": timestamp,
                "ended_at": timestamp,
                "messages": [
                    {
                        "role": message["role"],
                        "content": message["content"],
                        "timestamp": timestamp,
                    }
                    for message in messages
                ],
            }
        )
    db = SessionDB()
    try:
        result = db.import_sessions(sessions)
        if not result["ok"] or result["imported"] != len(sessions):
            raise RuntimeError(f"Complete history import failed: {result}")
        result["message_count"] = sum(len(s["messages"]) for s in sessions)
        args.receipt.write_text(json.dumps(result, indent=2))
        print(f"slate.eval: imported {result['imported']} complete histories")
    finally:
        db.close()


if __name__ == "__main__":
    main()
