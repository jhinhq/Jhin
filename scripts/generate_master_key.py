"""Generate a Jhin master key file (plan 13.2).

Usage:
    uv run python scripts/generate_master_key.py [path]

Default path: secrets/dev/jhin_master_key (used by the compose dev stack via
`make master-key`). Refuses to overwrite an existing key: replacing a master
key makes every secret encrypted under it permanently unreadable.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
from pathlib import Path

DEFAULT_PATH = Path("secrets/dev/jhin_master_key")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    if path.exists():
        print(f"refusing to overwrite existing master key at {path}")
        print("delete it manually first if you really intend to discard all stored secrets")
        raise SystemExit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    material = base64.b64encode(secrets.token_bytes(32)).decode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(material + "\n")
    print(f"master key written to {path} (mode 0600)")
    print("keep this file safe: losing it makes all stored secrets unreadable")


if __name__ == "__main__":
    main()
