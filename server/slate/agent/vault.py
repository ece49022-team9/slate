import asyncio
import json
import os
from pathlib import Path

from slate.agent import runtime

VAULT_SECONDS = 20
SCRIPT = """
import json
import sys

from agent.vault_store import VaultError, get_vault_store

request = json.load(sys.stdin)
store = get_vault_store()
try:
    if request["action"] == "add":
        item = store.add_item(
            request["kind"], request["label"], request["secret"], request["origin"]
        )
        print(json.dumps({"handle": item.id}))
    elif request["action"] == "remove":
        print(json.dumps({"removed": int(store.remove_item(request["handle"]))}))
    else:
        found = [
            item.id
            for item in store.list_items()
            if item.label.startswith(request["prefix"])
        ]
        for handle in found:
            store.remove_item(handle)
        print(json.dumps({"removed": len(found)}))
except VaultError as error:
    print(json.dumps({"error": str(error)}))
"""


class VaultError(RuntimeError):
    pass


class HermesVault:
    def __init__(self, python: Path | None = None, home: Path | None = None) -> None:
        default = runtime.SOURCE / ".venv/bin/python"
        self.python = python or Path(os.getenv("SLATE_HERMES_PYTHON") or default)
        self.home = home or runtime.PROFILE

    async def call(self, request: dict) -> dict:
        if not self.python.exists():
            raise VaultError("Hermes is not installed here; run make agent-setup")
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in ("PYTHONPATH", "VIRTUAL_ENV")
        }
        env["HERMES_HOME"] = str(self.home)
        process = await asyncio.create_subprocess_exec(
            str(self.python),
            "-I",
            "-c",
            SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(json.dumps(request).encode()), VAULT_SECONDS
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise VaultError("Hermes's vault did not answer in time") from None
        if process.returncode != 0:
            last = (err.decode(errors="replace").strip().splitlines() or ["no output"])[
                -1
            ]
            raise VaultError(f"Hermes's vault failed: {last[:200]}")
        reply = json.loads(out)
        if "error" in reply:
            raise VaultError(reply["error"])
        return reply

    async def add(
        self, kind: str, label: str, origin: str, secret: dict[str, str]
    ) -> str:
        reply = await self.call(
            {
                "action": "add",
                "kind": kind,
                "label": label,
                "origin": origin,
                "secret": secret,
            }
        )
        return reply["handle"]

    async def remove(self, handle: str) -> None:
        await self.call({"action": "remove", "handle": handle})

    async def purge(self, prefix: str) -> int:
        reply = await self.call({"action": "purge", "prefix": prefix})
        return reply["removed"]
