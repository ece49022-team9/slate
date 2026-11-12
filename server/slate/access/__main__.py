import argparse

from slate.access import home
from slate.access.credentials import CredentialStore
from slate.access.vault import Keyring


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m slate.access")
    parser.add_argument("command", choices=["rotate"])
    parser.parse_args()
    ring = Keyring.load()
    if ring.path is None:
        raise SystemExit("SLATE_MASTER_KEY is set; rotate that key where it is kept")
    old = set(ring.keys)
    new = ring.add_key()
    credentials = CredentialStore(home() / "credentials.sqlite3", ring)
    moved = credentials.rekey()
    for key in old:
        if key not in credentials.key_ids_in_use():
            ring.retire(key)
    credentials.close()
    print(
        f"new key {new}; re-encrypted {moved} secrets; keys left: {sorted(ring.keys)}"
    )


if __name__ == "__main__":
    main()
