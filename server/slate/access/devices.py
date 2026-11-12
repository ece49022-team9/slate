import hashlib
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    REAL NOT NULL,
    revoked_at    REAL,
    last_seen_at  REAL
);
"""
PREFIX = "sdt_"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    moment = datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")
    return moment.replace("+00:00", "Z")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    created_at: float
    revoked_at: float | None
    last_seen_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": iso(self.created_at),
            "revoked_at": iso(self.revoked_at),
            "last_seen_at": iso(self.last_seen_at),
        }


class DeviceStore:
    def __init__(
        self, path: str | Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(devices)")
        }
        if columns and "token_hash" not in columns:
            self.conn.execute("DROP TABLE devices")
        self.conn.executescript(SCHEMA)
        os.chmod(self.path, 0o600)

    def close(self) -> None:
        self.conn.close()

    def pair(self, name: str) -> tuple[Device, str]:
        device_id = "dev_" + secrets.token_hex(8)
        token = PREFIX + secrets.token_urlsafe(32)
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO devices (id, name, token_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (device_id, name, digest(token), self.clock()),
            )
        device = self.get(device_id)
        assert device is not None
        return device, token

    def identify(self, token: str) -> Device | None:
        if not token.startswith(PREFIX):
            return None
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL",
                (digest(token),),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                (self.clock(), row["id"]),
            )
        return self.get(row["id"])

    def get(self, device_id: str) -> Device | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        return None if row is None else to_device(row)

    def all(self) -> list[Device]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM devices ORDER BY created_at"
            ).fetchall()
        return [to_device(row) for row in rows]

    def revoke(self, device_id: str) -> Device | None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (self.clock(), device_id),
            )
        return self.get(device_id)


def to_device(row: sqlite3.Row) -> Device:
    return Device(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        last_seen_at=row["last_seen_at"],
    )
