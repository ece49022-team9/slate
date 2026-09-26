import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    subprocess.run(["bash", "scripts/esp-idf.sh", "build"], cwd=ROOT, check=True)
    with tempfile.TemporaryFile(mode="w+") as output:
        process = subprocess.Popen(
            ["bash", "scripts/esp-idf.sh", "qemu"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 60
        try:
            while time.monotonic() < deadline:
                output.seek(0)
                log = output.read()
                if "slate.boot: Firmware started." in log:
                    print(log, end="")
                    print("slate.firmware: ESP32-S3 boot passed")
                    return
                if process.poll() is not None:
                    raise RuntimeError(
                        f"slate.firmware: QEMU exited before boot\n{log}"
                    )
                time.sleep(0.1)
            output.seek(0)
            raise TimeoutError(f"slate.firmware: QEMU did not boot\n{output.read()}")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


if __name__ == "__main__":
    main()
