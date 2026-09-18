"""Bounded extraction subprocess entry point; no credentials or network are needed."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict


def main() -> None:
    if os.name == "posix":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    from jhin_media.files import MAX_FILE_BYTES, inspect_file

    try:
        data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        result = inspect_file(sys.argv[1], data)
        print(json.dumps({"result": asdict(result)}, ensure_ascii=True))
    except Exception:
        print(json.dumps({"error": "File could not be extracted within its supported limits"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
