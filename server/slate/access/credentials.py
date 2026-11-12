import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slate.access.vault import DecryptError, Keyring, Sealed
from slate.tasks.store import CredentialRef

KINDS: dict[str, tuple[str, ...]] = {
    "password": ("password",),
    "token": ("token",),
    "api_key": ("key",),
    "card": ("number", "expiry", "cvc"),
}
MAX_FIELDS = 8
MAX_VALUE = 4096

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id            TEXT PRIMARY KEY,
    service       TEXT NOT NULL,
    label         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    username      TEXT,
    hint          TEXT,
    key_id        TEXT NOT NULL,
    nonce         BLOB NOT NULL,
    ciphertext    BLOB NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    last_used_at  REAL,
    use_count     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS credential_uses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id  TEXT NOT NULL,
    task_id        TEXT,
    purpose        TEXT NOT NULL,
    at             REAL NOT NULL,
    allowed        INTEGER NOT NULL,
    reason         TEXT,
    approval_id    TEXT
);
CREATE INDEX IF NOT EXISTS credential_uses_by_id ON credential_uses (credential_id, id);
"""


class CredentialNotFound(Exception):
    pass


class BadSecret(ValueError):
    pass


class AccessDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Credential:
    id: str
    service: str
    label: str
    kind: str
    username: str | None
    hint: str | None
    key_id: str
    created_at: float
    updated_at: float
    last_used_at: float | None
    use_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "service": self.service,
            "label": self.label,
            "kind": self.kind,
            "username": self.username,
            "hint": self.hint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
        }


@dataclass(frozen=True)
class Secret:
    credential_id: str
    username: str | None
    values: dict[str, str] = field(repr=False)

    def __getitem__(self, name: str) -> str:
        return self.values[name]


@dataclass(frozen=True)
class Use:
    credential_id: str
    task_id: str | None
    purpose: str
    at: float
    allowed: bool
    reason: str | None
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "purpose": self.purpose,
            "at": self.at,
            "allowed": self.allowed,
            "reason": self.reason,
            "approval_id": self.approval_id,
        }


def check_secret(kind: str, values: Any) -> dict[str, str]:
    if kind not in KINDS:
        raise BadSecret(f"Choose a kind from {', '.join(KINDS)}")
    if not isinstance(values, dict):
        raise BadSecret("Send the secret as an object of text fields")
    if len(values) > MAX_FIELDS:
        raise BadSecret(f"A secret has at most {MAX_FIELDS} fields")
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise BadSecret(f"Field {name!r} must be text")
        if not value or len(value) > MAX_VALUE:
            raise BadSecret(f"Field {name!r} must be 1 to {MAX_VALUE} characters")
    missing = [name for name in KINDS[kind] if name not in values]
    if missing:
        raise BadSecret(f"A {kind} needs {', '.join(missing)}")
    return dict(values)


def make_hint(kind: str, values: dict[str, str]) -> str | None:
    if kind == "card":
        digits = "".join(ch for ch in values["number"] if ch.isdigit())
        return f"•••• {digits[-4:]}" if len(digits) >= 4 else None
    return None


def aad(credential_id: str, service: str, kind: str) -> bytes:
    return f"slate-credential-v1\n{credential_id}\n{service}\n{kind}".encode()


class CredentialStore:
    def __init__(
        self,
        path: str | Path,
        keyring: Keyring,
        *,
        authorize: Callable[[str, str], tuple[str | None, str | None]] = (
            lambda task_id, credential_id: (None, "no task checker")
        ),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.keyring = keyring
        self.authorize = authorize
        self.clock = clock
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(credential_uses)")
        }
        if "approval_id" not in columns:
            self.conn.execute("ALTER TABLE credential_uses ADD COLUMN approval_id TEXT")
        self.path.chmod(0o600)

    def close(self) -> None:
        self.conn.close()

    def add(
        self,
        service: str,
        label: str,
        kind: str,
        secret: Any,
        username: str | None = None,
    ) -> Credential:
        values = check_secret(kind, secret)
        credential_id = "cred_" + secrets.token_hex(8)
        sealed = self.keyring.seal(
            json.dumps(values).encode(), aad(credential_id, service, kind)
        )
        now = self.clock()
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO credentials (id, service, label, kind, username, hint, "
                "key_id, nonce, ciphertext, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    credential_id,
                    service,
                    label,
                    kind,
                    username,
                    make_hint(kind, values),
                    sealed.key_id,
                    sealed.nonce,
                    sealed.ciphertext,
                    now,
                    now,
                ),
            )
        return self.get(credential_id)

    def get(self, credential_id: str) -> Credential:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM credentials WHERE id = ?", (credential_id,)
            ).fetchone()
        if row is None:
            raise CredentialNotFound(credential_id)
        return _credential(row)

    def describe(self, credential_id: str) -> CredentialRef:
        item = self.get(credential_id)
        return CredentialRef(item.id, item.service, item.label)

    def all(self) -> list[Credential]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM credentials ORDER BY service, label"
            ).fetchall()
        return [_credential(row) for row in rows]

    def rename(
        self,
        credential_id: str,
        *,
        label: str | None = None,
        username: str | None = None,
    ) -> Credential:
        current = self.get(credential_id)
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE credentials SET label = ?, username = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    label if label is not None else current.label,
                    username if username is not None else current.username,
                    self.clock(),
                    credential_id,
                ),
            )
        return self.get(credential_id)

    def replace_secret(self, credential_id: str, secret: Any) -> Credential:
        current = self.get(credential_id)
        values = check_secret(current.kind, secret)
        sealed = self.keyring.seal(
            json.dumps(values).encode(),
            aad(credential_id, current.service, current.kind),
        )
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE credentials SET hint = ?, key_id = ?, nonce = ?, "
                "ciphertext = ?, updated_at = ? WHERE id = ?",
                (
                    make_hint(current.kind, values),
                    sealed.key_id,
                    sealed.nonce,
                    sealed.ciphertext,
                    self.clock(),
                    credential_id,
                ),
            )
        return self.get(credential_id)

    def delete(self, credential_id: str) -> None:
        self.get(credential_id)
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))

    def uses(self, credential_id: str, limit: int = 50) -> list[Use]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM credential_uses WHERE credential_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (credential_id, limit),
            ).fetchall()
        return [
            Use(
                credential_id=row["credential_id"],
                task_id=row["task_id"],
                purpose=row["purpose"],
                at=row["at"],
                allowed=bool(row["allowed"]),
                reason=row["reason"],
                approval_id=row["approval_id"],
            )
            for row in rows
        ]

    def use(self, credential_id: str, *, task_id: str, purpose: str) -> Secret:
        try:
            credential = self.get(credential_id)
        except CredentialNotFound:
            self._log(credential_id, task_id, purpose, False, "not_found")
            raise AccessDenied("not_found") from None
        approval_id, refusal = self.authorize(task_id, credential_id)
        if refusal is not None or approval_id is None:
            refusal = refusal or "not_granted"
            self._log(credential_id, task_id, purpose, False, refusal)
            raise AccessDenied(refusal)
        with self.lock:
            row = self.conn.execute(
                "SELECT key_id, nonce, ciphertext FROM credentials WHERE id = ?",
                (credential_id,),
            ).fetchone()
        try:
            plain = self.keyring.open(
                Sealed(row["key_id"], row["nonce"], row["ciphertext"]),
                aad(credential_id, credential.service, credential.kind),
            )
        except DecryptError as error:
            self._log(credential_id, task_id, purpose, False, error.reason)
            raise AccessDenied(error.reason) from None
        now = self.clock()
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE credentials SET last_used_at = ?, use_count = use_count + 1 "
                "WHERE id = ?",
                (now, credential_id),
            )
        self._log(credential_id, task_id, purpose, True, None, approval_id)
        return Secret(credential_id, credential.username, json.loads(plain))

    def rekey(self) -> int:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, service, kind, key_id, nonce, ciphertext FROM credentials "
                "WHERE key_id != ?",
                (self.keyring.current,),
            ).fetchall()
        for row in rows:
            data = aad(row["id"], row["service"], row["kind"])
            plain = self.keyring.open(
                Sealed(row["key_id"], row["nonce"], row["ciphertext"]), data
            )
            sealed = self.keyring.seal(plain, data)
            with self.lock, self.conn:
                self.conn.execute(
                    "UPDATE credentials SET key_id = ?, nonce = ?, ciphertext = ? "
                    "WHERE id = ?",
                    (sealed.key_id, sealed.nonce, sealed.ciphertext, row["id"]),
                )
        return len(rows)

    def key_ids_in_use(self) -> set[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT key_id FROM credentials"
            ).fetchall()
        return {row["key_id"] for row in rows}

    def _log(
        self,
        credential_id: str,
        task_id: str | None,
        purpose: str,
        allowed: bool,
        reason: str | None,
        approval_id: str | None = None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO credential_uses "
                "(credential_id, task_id, purpose, at, allowed, reason, approval_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    credential_id,
                    task_id,
                    purpose,
                    self.clock(),
                    int(allowed),
                    reason,
                    approval_id,
                ),
            )


def _credential(row: sqlite3.Row) -> Credential:
    return Credential(
        id=row["id"],
        service=row["service"],
        label=row["label"],
        kind=row["kind"],
        username=row["username"],
        hint=row["hint"],
        key_id=row["key_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
        use_count=row["use_count"],
    )
