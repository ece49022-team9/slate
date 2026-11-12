"""Encryption at rest for secrets: AES-256-GCM under a master key.

Every secret gets its own random 12-byte nonce. GCM also checks integrity, so
a changed byte in the database makes decryption fail instead of returning
garbage. Each secret is bound to where it belongs ("associated data", e.g.
its credential id and service): copying one row's ciphertext into another
row fails to decrypt.

The master key does not live in the database. It comes from SLATE_MASTER_KEY
(64 hex chars) or from a key file outside ~/.slate (default
~/.config/slate/master.key, mode 600). Someone who copies the database alone
gets nothing readable.

Keys have ids (first 8 hex chars of SHA-256 of the key), and every sealed
value records which key sealed it. That makes rotation possible: add a new
key, re-encrypt everything, then retire the old one.
"""

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENV = "SLATE_MASTER_KEY"
DEFAULT_KEY_FILE = Path.home() / ".config" / "slate" / "master.key"


class DecryptError(Exception):
    """unknown_key: sealed with a key we no longer have.
    tampered: the ciphertext, nonce, or associated data does not match."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Sealed:
    key_id: str
    nonce: bytes
    ciphertext: bytes  # includes the 16-byte GCM tag


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


class Keyring:
    def __init__(
        self, keys: list[bytes], *, current: str | None = None, path: Path | None = None
    ) -> None:
        if not keys:
            raise ValueError("need at least one key")
        for key in keys:
            if len(key) != 32:
                raise ValueError("keys must be 32 bytes")
        self.keys = {key_id(key): key for key in keys}
        self.current = current or key_id(keys[-1])
        if self.current not in self.keys:
            raise ValueError("current key is not in the keyring")
        self.path = path  # where to save changes; None for env or test keys

    @classmethod
    def load(cls, path: Path | None = None) -> "Keyring":
        """SLATE_MASTER_KEY wins. Otherwise read the key file, making it on
        first run."""
        env = os.getenv(ENV)
        if env:
            return cls([bytes.fromhex(env)])
        path = Path(path or DEFAULT_KEY_FILE)
        if not path.exists():
            ring = cls([secrets.token_bytes(32)], path=path)
            ring.save()
            return ring
        data = json.loads(path.read_text())
        return cls(
            [bytes.fromhex(k) for k in data["keys"]],
            current=data["current"],
            path=path,
        )

    def save(self) -> None:
        if self.path is None:
            raise RuntimeError(
                "this keyring came from the environment; nothing to save"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # Create with mode 600 from the start, so it is never readable by others.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(
                {
                    "current": self.current,
                    "keys": [k.hex() for k in self.keys.values()],
                },
                out,
            )
        os.replace(tmp, self.path)

    def seal(self, plaintext: bytes, aad: bytes) -> Sealed:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self.keys[self.current]).encrypt(nonce, plaintext, aad)
        return Sealed(self.current, nonce, ciphertext)

    def open(self, sealed: Sealed, aad: bytes) -> bytes:
        key = self.keys.get(sealed.key_id)
        if key is None:
            raise DecryptError("unknown_key")
        try:
            return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, aad)
        except InvalidTag:
            raise DecryptError("tampered") from None

    # Rotation

    def add_key(self) -> str:
        """Make a new key the current one. Old keys stay for reading."""
        key = secrets.token_bytes(32)
        self.keys[key_id(key)] = key
        self.current = key_id(key)
        if self.path is not None:
            self.save()
        return self.current

    def retire(self, old: str) -> None:
        if old == self.current:
            raise ValueError("cannot retire the current key")
        del self.keys[old]
        if self.path is not None:
            self.save()
