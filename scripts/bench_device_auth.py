"""How much time does checking a signature add? Runs verify() directly.

uv run python scripts/bench_device_auth.py
"""

import statistics
import tempfile
import time
from pathlib import Path

from slate.access.devices import DeviceStore
from slate.access.signing import Verifier, signed_headers

N = 5000


def run(verifier, device, secret, body):
    times = []
    for _ in range(N):
        headers = {
            k.lower(): v
            for k, v in signed_headers(
                device.id, secret, "POST", "/api/device/approvals/apr_1", body
            ).items()
        }
        start = time.perf_counter()
        verifier.verify("POST", "/api/device/approvals/apr_1", headers, body)
        times.append((time.perf_counter() - start) * 1e6)
    times.sort()
    return statistics.median(times), times[int(0.99 * len(times))]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = DeviceStore(Path(tmp) / "d.db")
        device, secret = store.pair("bench")
        # Bigger window and limit so the bench measures checking, not the limit.
        verifier = Verifier(store, max_nonces=10 * N)
        for size in (0, 1024, 16 * 1024):
            body = b"x" * size
            median, p99 = run(verifier, device, secret, body)
            print(f"body {size:>6} B: median {median:6.1f} us, p99 {p99:6.1f} us")
        print(f"nonces remembered: {verifier.remembered()}")
        store.close()


if __name__ == "__main__":
    main()
