"""Paired devices and their signing secrets.

Pairing makes a device id and a random 32-byte secret. The secret is shown
once, to be copied onto the device, and is never returned by the API again.
HMAC needs the server to know the secret itself (a hash would not work), so
with a keyring the secret is encrypted at rest (see slate.access.vault) and
only decrypted to check a signature. Rows written before encryption was
turned on are encrypted the first time the store opens with a keyring.
"""

import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slate.access.vault import Keyring, Sealed

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    secret        BLOB NOT NULL,  -- ciphertext when secret_nonce is set
    created_at    REAL NOT NULL,
    revoked_at    REAL,
    last_seen_at  REAL,
    secret_key_id TEXT,
    secret_nonce  BLOB
);
"""


def aad(device_id: str) -> bytes:
    return f"slate-device-v1\n{device_id}".encode()


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    created_at: float
    revoked_at: float | None
    last_seen_at: float | None
    secret: bytes = field(repr=False)  # keep it out of logs and tracebacks

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
            "last_seen_at": self.last_seen_at,
        }


class DeviceStore:
    def __init__(
        self,
        path: str | Path,
        *,
        keyring: Keyring | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(devices)")
        }
        for name, kind in (("secret_key_id", "TEXT"), ("secret_nonce", "BLOB")):
            if name not in columns:  # database from before encryption
                self.conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {kind}")
        os.chmod(self.path, 0o600)
        self.keyring = keyring
        if keyring is not None:
            self.encrypt_plain_rows()

    def close(self) -> None:
        self.conn.close()

    def pair(self, name: str) -> tuple[Device, bytes]:
        device_id = "dev_" + secrets.token_hex(8)
        secret = secrets.token_bytes(32)
        stored, key, nonce = secret, None, None
        if self.keyring is not None:
            sealed = self.keyring.seal(secret, aad(device_id))
            stored, key, nonce = sealed.ciphertext, sealed.key_id, sealed.nonce
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO devices "
                "(id, name, secret, secret_key_id, secret_nonce, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (device_id, name, stored, key, nonce, self.clock()),
            )
        device = self.get(device_id)
        assert device is not None
        return device, secret

    def get(self, device_id: str) -> Device | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        return None if row is None else self._device(row)

    def all(self) -> list[Device]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM devices ORDER BY created_at"
            ).fetchall()
        return [self._device(row) for row in rows]

    def revoke(self, device_id: str) -> Device | None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (self.clock(), device_id),
            )
        return self.get(device_id)

    def touch(self, device_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                (self.clock(), device_id),
            )

    def encrypt_plain_rows(self) -> int:
        """Encrypt rows from before the keyring, and re-encrypt rows under an
        old key (after rotation). Returns how many rows changed."""
        assert self.keyring is not None
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM devices "
                "WHERE secret_nonce IS NULL OR secret_key_id != ?",
                (self.keyring.current,),
            ).fetchall()
        for row in rows:
            secret = self._secret(row)
            sealed = self.keyring.seal(secret, aad(row["id"]))
            with self.lock, self.conn:
                self.conn.execute(
                    "UPDATE devices "
                    "SET secret = ?, secret_key_id = ?, secret_nonce = ? "
                    "WHERE id = ?",
                    (sealed.ciphertext, sealed.key_id, sealed.nonce, row["id"]),
                )
        return len(rows)

    def _secret(self, row: sqlite3.Row) -> bytes:
        if row["secret_nonce"] is None:
            return bytes(row["secret"])
        if self.keyring is None:
            raise RuntimeError(
                "device secrets are encrypted; open the store with a keyring"
            )
        return self.keyring.open(
            Sealed(row["secret_key_id"], row["secret_nonce"], bytes(row["secret"])),
            aad(row["id"]),
        )

    def _device(self, row: sqlite3.Row) -> Device:
        return Device(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
            last_seen_at=row["last_seen_at"],
            secret=self._secret(row),
        )
