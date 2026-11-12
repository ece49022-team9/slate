import argparse
import os
from pathlib import Path

from slate.access.credentials import CredentialStore
from slate.access.devices import DeviceStore
from slate.access.vault import Keyring


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m slate.access")
    parser.add_argument("command", choices=["rotate"])
    parser.parse_args()

    home = Path.home() / ".slate"
    ring = Keyring.load()
    if ring.path is None:
        raise SystemExit("SLATE_MASTER_KEY is set; rotate that key where it is kept")
    old = set(ring.keys)
    new = ring.add_key()
    credentials = CredentialStore(
        os.getenv("SLATE_CREDENTIALS_DB") or home / "credentials.sqlite3", ring
    )
    devices = DeviceStore(os.getenv("SLATE_DEVICES_DB") or home / "devices.sqlite3")
    devices.keyring = ring
    moved = credentials.rekey() + devices.encrypt_plain_rows()
    for key in old:
        if key not in credentials.key_ids_in_use():
            ring.retire(key)
    credentials.close()
    devices.close()
    print(
        f"new key {new}; re-encrypted {moved} secrets; keys left: {sorted(ring.keys)}"
    )


if __name__ == "__main__":
    main()
