import argparse
import json
import secrets
import sys
import time
from pathlib import Path

import httpx
from slate.access.signing import signed_headers

SAVED = Path(".local/device.json")


def show(label: str, reply: httpx.Response) -> None:
    print(f"{label}: {reply.status_code} {reply.text}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["pair", "ping", "approve", "deny", "cancel"]
    )
    parser.add_argument("target", nargs="?")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--tamper", action="store_true")
    parser.add_argument("--skew", type=int, default=0)
    parser.add_argument("--wrong-key", action="store_true")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.server, timeout=5)

    if args.command == "pair":
        reply = client.post("/api/devices", json={"name": args.target or "Slate"})
        reply.raise_for_status()
        made = reply.json()
        SAVED.parent.mkdir(exist_ok=True)
        SAVED.write_text(
            json.dumps({"id": made["device"]["id"], "secret": made["secret"]})
        )
        SAVED.chmod(0o600)
        print(f"paired {made['device']['id']}; secret saved to {SAVED}")
        return 0

    saved = json.loads(SAVED.read_text())
    device_id = saved["id"]
    secret = (
        secrets.token_bytes(32) if args.wrong_key else bytes.fromhex(saved["secret"])
    )

    if args.command == "ping":
        method, path, payload = "GET", "/api/device/ping", None
    elif args.command == "cancel":
        method, path, payload = "POST", f"/api/device/tasks/{args.target}/cancel", None
    else:
        method, path = "POST", f"/api/device/approvals/{args.target}"
        payload = {"decision": args.command}

    body = b"" if payload is None else json.dumps(payload).encode()
    signed_body = body
    if args.tamper and payload is not None:
        flipped = "deny" if args.command == "approve" else "approve"
        signed_body = json.dumps({"decision": flipped}).encode()
    headers = signed_headers(
        device_id,
        secret,
        method,
        path,
        signed_body,
        timestamp=int(time.time()) + args.skew,
    )
    first = client.request(method, path, content=body, headers=headers)
    show("request", first)
    if args.replay:
        show("replay ", client.request(method, path, content=body, headers=headers))
    return 0 if first.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
