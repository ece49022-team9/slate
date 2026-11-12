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
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Sealed:
    key_id: str
    nonce: bytes
    ciphertext: bytes


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


class Keyring:
    def __init__(
        self, keys: list[bytes], *, current: str | None = None, path: Path | None = None
    ) -> None:
        if not keys:
            raise ValueError("The keyring needs at least one key")
        for key in keys:
            if len(key) != 32:
                raise ValueError("Keys must be 32 bytes")
        self.keys = {key_id(key): key for key in keys}
        self.current = current or key_id(keys[-1])
        if self.current not in self.keys:
            raise ValueError("The current key is not in the keyring")
        self.path = path

    @classmethod
    def load(cls, path: Path | None = None) -> "Keyring":
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

    def add_key(self) -> str:
        key = secrets.token_bytes(32)
        self.keys[key_id(key)] = key
        self.current = key_id(key)
        if self.path is not None:
            self.save()
        return self.current

    def retire(self, old: str) -> None:
        if old == self.current:
            raise ValueError("The current key cannot be retired")
        del self.keys[old]
        if self.path is not None:
            self.save()
