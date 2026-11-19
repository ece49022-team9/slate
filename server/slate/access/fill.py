import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from slate.access.credentials import Credential, Secret

logger = logging.getLogger("slate.access.fill")
VAULT_KINDS = {"password": "login", "card": "payment"}
SPOKEN_KINDS = {"password": "login", "card": "card"}
LABEL_PREFIX = "Slate: "
EXPIRY = re.compile(r"^\s*(\d{1,2})\s*[/-]\s*(\d{2}|\d{4})\s*$")
PHONE = re.compile(r"^\+?[\d\s().-]{7,}$")
CARD_EXTRAS = {"name": "cardholder_name", "postal_code": "billing_postal_code"}


class CannotFill(ValueError):
    pass


class Vault(Protocol):
    async def add(
        self, kind: str, label: str, origin: str, secret: dict[str, str]
    ) -> str: ...

    async def remove(self, handle: str) -> None: ...

    async def purge(self, prefix: str) -> int: ...


def check_fillable(credential: Credential, site: str) -> None:
    if credential.kind not in VAULT_KINDS:
        raise CannotFill(
            f"Slate fills only logins and cards into a page, not a {credential.kind}"
        )
    if credential.kind == "password" and not credential.username:
        raise CannotFill("Add the username for this login on the dashboard first")
    if credential.origin is not None and credential.origin != site:
        raise CannotFill(
            f"This login is saved for {credential.origin}, not {site}; "
            "open that site and try again"
        )


def identifier_type(username: str) -> str:
    if "@" in username:
        return "email"
    if PHONE.match(username):
        return "phone"
    return "username"


def vault_secret(credential: Credential, secret: Secret) -> dict[str, str]:
    if credential.kind == "password":
        assert credential.username is not None
        return {
            "identifier_type": identifier_type(credential.username),
            "identifier": credential.username,
            "password": secret["password"],
        }
    found = EXPIRY.match(secret["expiry"])
    if found is None:
        raise CannotFill("Save the card expiry as MM/YY on the dashboard first")
    month, year = found.groups()
    values = {
        "card_number": "".join(ch for ch in secret["number"] if ch.isdigit()),
        "exp_month": month.zfill(2),
        "exp_year": year if len(year) == 4 else f"20{year}",
        "cvc": secret["cvc"],
    }
    for name, field in CARD_EXTRAS.items():
        if secret.values.get(name):
            values[field] = secret.values[name]
    return values


@dataclass
class Release:
    site: str
    handle: str | None = None


class Releases:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.held: dict[str, dict[str, Release]] = {}
        self.lock = asyncio.Lock()
        self.cleanup: set[asyncio.Task] = set()

    def site(self, task_id: str, credential_id: str) -> str | None:
        release = self.held.get(task_id, {}).get(credential_id)
        return None if release is None else release.site

    def allow(self, task_id: str, credential_id: str, site: str) -> None:
        self.held.setdefault(task_id, {}).setdefault(credential_id, Release(site))

    async def fill(
        self, task_id: str, credential: Credential, secret: Secret, site: str
    ) -> str:
        async with self.lock:
            self.allow(task_id, credential.id, site)
            release = self.held[task_id][credential.id]
            if release.handle is None:
                release.handle = await self.vault.add(
                    VAULT_KINDS[credential.kind],
                    LABEL_PREFIX + credential.label,
                    site,
                    vault_secret(credential, secret),
                )
                logger.info(
                    "Put credential %s in the browser vault for task %s on %s",
                    credential.id,
                    task_id,
                    site,
                )
            return release.handle

    def finish(self, task_id: str) -> None:
        releases = self.held.pop(task_id, {})
        handles = [item.handle for item in releases.values() if item.handle]
        if not handles:
            return
        task = asyncio.get_running_loop().create_task(self.remove(task_id, handles))
        self.cleanup.add(task)
        task.add_done_callback(self.cleanup.discard)

    async def remove(self, task_id: str, handles: list[str]) -> None:
        for handle in handles:
            try:
                await self.vault.remove(handle)
            except Exception:
                logger.exception(
                    "Could not take %s out of the browser vault for task %s",
                    handle,
                    task_id,
                )
        logger.info("Took task %s's credentials out of the browser vault", task_id)

    async def purge(self) -> None:
        try:
            removed = await self.vault.purge(LABEL_PREFIX)
        except Exception as error:
            logger.info("Browser vault not cleaned at start: %s", error)
            return
        if removed:
            logger.info(
                "Took %s leftover credentials out of the browser vault", removed
            )

    async def close(self) -> None:
        for task_id in list(self.held):
            self.finish(task_id)
        if self.cleanup:
            await asyncio.gather(*self.cleanup, return_exceptions=True)
