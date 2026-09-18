"""Copied into an unprivileged, offline container; never executed on the host."""

import base64
import contextlib
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

LIMIT = 25 * 1024 * 1024
TOTAL = 32 * 1024 * 1024
EXCLUDED = {
    ".git",
    "node_modules",
    ".next",
    ".venv",
    "venv",
    "__pycache__",
    ".jhin",
    "dist",
    "build",
}


def path_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid workspace path")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or any(part in {"..", ".git"} for part in path.parts):
        raise ValueError("workspace path escapes its root")
    return path.parts


def parent_fd(root: str | os.PathLike[str], value: str, create: bool = False) -> tuple[int, str]:
    parts = path_parts(value)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def read_file(root: str | os.PathLike[str], path: str) -> bytes:
    fd, name = parent_fd(root, path)
    try:
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(child)
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                raise ValueError("file is not regular or exceeds 25 MiB")
            with os.fdopen(child, "rb", closefd=False) as stream:
                data = stream.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError("file exceeds 25 MiB")
            return data
        finally:
            os.close(child)
    finally:
        os.close(fd)


def digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def current(root: str | os.PathLike[str], path: str) -> bytes | None:
    try:
        return read_file(root, path)
    except FileNotFoundError:
        return None


def write_file(root: str | os.PathLike[str], path: str, data: bytes | None) -> None:
    fd, name = parent_fd(root, path, create=data is not None)
    temporary = ".jhin-write-" + os.urandom(8).hex()
    try:
        if data is None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=fd)
            return
        file_fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        with os.fdopen(file_fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=fd)
        os.close(fd)


def execute(payload: dict[str, Any], root: str | os.PathLike[str] = "/workspace") -> dict[str, Any]:
    data: bytes | None
    operation, args = payload["operation"], payload.get("args", {})
    if operation == "browse":
        path = args.get("path", "")
        if not isinstance(path, str) or len(path) > 1024:
            raise ValueError("invalid workspace directory")
        if path:
            # Open every ancestor without following links. A rename while
            # browsing cannot redirect this directory descriptor outside root.
            fd, _ = parent_fd(root, path.rstrip("/") + "/.jhin-browse-anchor")
        else:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        items: list[dict[str, Any]] = []
        examined, omitted, truncated = 0, 0, False
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    examined += 1
                    if len(items) >= 256 or examined > 1024:
                        truncated = True
                        break
                    if entry.name in EXCLUDED or entry.name.startswith(".env"):
                        omitted += 1
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                        omitted += 1
                        continue
                    items.append(
                        {
                            "name": entry.name,
                            "path": str(PurePosixPath(path) / entry.name),
                            "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                            "size_bytes": info.st_size if stat.S_ISREG(info.st_mode) else None,
                        }
                    )
        finally:
            os.close(fd)
        items.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].lower()))
        return {"items": items, "path": path, "truncated": truncated, "omitted": omitted}
    if operation == "stage":
        data = base64.b64decode(args["content_base64"], validate=True)
        if len(data) > LIMIT:
            raise ValueError("file exceeds 25 MiB")
        before = current(root, args["path"])
        if before is not None:
            if digest(before) != digest(data):
                raise ValueError("revision_conflict: staged input was changed; original retained")
            return {"sha256": digest(before), "size_bytes": len(before), "created": False}
        write_file(root, args["path"], data)
        return {"sha256": digest(data), "size_bytes": len(data), "created": True}
    if operation == "read":
        if args.get("allow_missing") is True:
            value = current(root, args["path"])
            if value is None:
                return {"path": args["path"], "missing": True}
        data = read_file(root, args["path"])
        return {
            "content_base64": base64.b64encode(data).decode(),
            "sha256": digest(data),
            "size_bytes": len(data),
        }
    if operation in {"write", "restore"}:
        values = args.get("files", []) if operation == "restore" else [args]
        if not isinstance(values, list) or not 1 <= len(values) <= 256:
            raise ValueError("invalid write set")
        prepared: list[tuple[str, bytes | None, bytes | None]] = []
        paths: set[str] = set()
        size = 0
        for value in values:
            path = value["path"]
            path_parts(path)
            if path in paths:
                raise ValueError("duplicate write path")
            paths.add(path)
            before = current(root, path)
            if digest(before) != value.get("expected_sha256"):
                raise ValueError("revision_conflict: workspace file changed")
            encoded = value.get("content_base64")
            data = base64.b64decode(encoded, validate=True) if encoded is not None else None
            size += len(data or b"") + len(before or b"")
            if len(data or b"") > LIMIT or size > TOTAL * 2:
                raise ValueError("write set exceeds limit")
            prepared.append((path, before, data))
        completed = []
        try:
            for path, before, data in prepared:
                write_file(root, path, data)
                completed.append((path, before))
        except BaseException:
            for path, before in reversed(completed):
                write_file(root, path, before)
            raise
        return {"restored": [p for p, _, _ in prepared], "sha256": digest(prepared[0][2])}
    if operation in {"snapshot", "list"}:
        files: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        size = 0
        for directory, dirs, names in os.walk(root, followlinks=False):
            for name in list(dirs):
                candidate = Path(directory) / name
                relative = candidate.relative_to(root).as_posix()
                if name in EXCLUDED or candidate.is_symlink() or relative.count("/") > 20:
                    dirs.remove(name)
                    excluded.append({"path": relative, "reason": "excluded directory or symlink"})
            for name in sorted(names):
                path = (Path(directory) / name).relative_to(root).as_posix()
                if name.startswith(".env"):
                    excluded.append({"path": path, "reason": "environment file"})
                    continue
                if len(files) >= 256:
                    excluded.append({"path": path, "reason": "file count limit"})
                    continue
                try:
                    data = read_file(root, path)
                    if size + len(data) > TOTAL:
                        raise ValueError("snapshot byte limit")
                    snapshot_entry: dict[str, Any] = {
                        "path": path,
                        "sha256": digest(data),
                        "size_bytes": len(data),
                    }
                    if operation == "snapshot":
                        snapshot_entry["content_base64"] = base64.b64encode(data).decode()
                    files.append(snapshot_entry)
                    size += len(data)
                except (OSError, ValueError) as error:
                    excluded.append({"path": path, "reason": type(error).__name__})
        return {"files": files, "excluded": excluded[:1000]}
    raise ValueError("unsupported workspace operation")


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.buffer.readline(48 * 1024 * 1024 + 1))
        print(json.dumps({"ok": True, "data": execute(request)}, separators=(",", ":")))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "error": str(error)[:200]}))
        sys.exit(2)
