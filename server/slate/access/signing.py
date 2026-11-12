import hashlib
import heapq
import hmac
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from slate.access.devices import DeviceStore

SCHEME = "SLATE-HMAC-SHA256-V1"
WINDOW = 30
MAX_NONCES = 600

DEVICE_HEADER = "x-slate-device"
TIMESTAMP_HEADER = "x-slate-timestamp"
NONCE_HEADER = "x-slate-nonce"
SIGNATURE_HEADER = "x-slate-signature"

DEVICE_ID = re.compile(r"dev_[0-9a-f]{16}")
TIMESTAMP = re.compile(r"[0-9]{1,12}")
NONCE = re.compile(r"[0-9a-f]{32}")
SIGNATURE = re.compile(r"[0-9a-f]{64}")


def canonical(
    method: str, path: str, timestamp: int, nonce: str, device_id: str, body: bytes
) -> bytes:
    return "\n".join(
        [
            SCHEME,
            method.upper(),
            path,
            str(timestamp),
            nonce,
            device_id,
            hashlib.sha256(body).hexdigest(),
        ]
    ).encode()


def sign(
    secret: bytes,
    method: str,
    path: str,
    timestamp: int,
    nonce: str,
    device_id: str,
    body: bytes = b"",
) -> str:
    message = canonical(method, path, timestamp, nonce, device_id, body)
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def signed_headers(
    device_id: str,
    secret: bytes,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = secrets.token_hex(16) if nonce is None else nonce
    return {
        "X-Slate-Device": device_id,
        "X-Slate-Timestamp": str(timestamp),
        "X-Slate-Nonce": nonce,
        "X-Slate-Signature": sign(
            secret, method, path, timestamp, nonce, device_id, body
        ),
    }


class AuthError(Exception):
    def __init__(self, reason: str, status: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class _Seen:
    nonces: set[str]
    expiry: list[tuple[float, str]]


class Verifier:
    def __init__(
        self,
        devices: DeviceStore,
        *,
        window: int = WINDOW,
        max_nonces: int = MAX_NONCES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.devices = devices
        self.window = window
        self.max_nonces = max_nonces
        self.clock = clock
        self.not_before = math.floor(clock())
        self.seen: dict[str, _Seen] = {}
        self.lock = threading.Lock()
        self.stats: dict[str, int] = {"accepted": 0}

    def verify(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> str:
        try:
            return self._check(method, path, headers, body)
        except AuthError as error:
            with self.lock:
                self.stats[error.reason] = self.stats.get(error.reason, 0) + 1
            raise

    def _check(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> str:
        device_id = headers.get(DEVICE_HEADER, "")
        stamp = headers.get(TIMESTAMP_HEADER, "")
        nonce = headers.get(NONCE_HEADER, "")
        signature = headers.get(SIGNATURE_HEADER, "")
        if not (
            DEVICE_ID.fullmatch(device_id)
            and TIMESTAMP.fullmatch(stamp)
            and NONCE.fullmatch(nonce)
            and SIGNATURE.fullmatch(signature)
        ):
            raise AuthError("bad_format")

        timestamp = int(stamp)
        now = self.clock()
        if abs(now - timestamp) > self.window:
            raise AuthError("clock_skew")
        if timestamp < self.not_before:
            raise AuthError("before_boot")

        device = self.devices.get(device_id)
        if device is None:
            raise AuthError("unknown_device")
        if device.revoked_at is not None:
            raise AuthError("revoked")

        expected = sign(device.secret, method, path, timestamp, nonce, device_id, body)
        if not hmac.compare_digest(expected, signature):
            raise AuthError("bad_signature")

        with self.lock:
            seen = self.seen.setdefault(device_id, _Seen(set(), []))
            while seen.expiry and seen.expiry[0][0] < now:
                _, old = heapq.heappop(seen.expiry)
                seen.nonces.discard(old)
            if nonce in seen.nonces:
                raise AuthError("replay")
            if len(seen.nonces) >= self.max_nonces:
                raise AuthError("rate_limited", status=429)
            seen.nonces.add(nonce)
            heapq.heappush(seen.expiry, (timestamp + self.window, nonce))
            self.stats["accepted"] += 1
        return device_id

    def remembered(self) -> int:
        with self.lock:
            return sum(len(seen.nonces) for seen in self.seen.values())
