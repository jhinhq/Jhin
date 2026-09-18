"""Local, immutable workspace blobs and bounded, non-executing file inspection.

Blob keys are computed from bytes, never supplied filesystem paths. Published
versions outlive sandbox containers. This store deliberately has no avatar
normalization and never evaluates HTML, Office macros, or archive members.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from PIL import Image, UnidentifiedImageError

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_CHARS = 200_000
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 2000


class InvalidFile(ValueError):
    """The supplied name, bytes or storage key is not a supported safe file."""


class StorageQuotaExceeded(InvalidFile):
    """New blob publication would exceed the retained workspace storage limit."""


@contextmanager
def _workspace_blob_lock(directory: Path) -> Iterator[None]:
    """Serialize API/worker writers sharing the local volume, without DB lock inversion."""
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".quota.lock"
    if lock.is_symlink():
        raise InvalidFile("Storage lock may not be a symlink")
    with lock.open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def validate_relative_path(value: str) -> str:
    if (
        not value
        or len(value) > 1024
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
        or value.startswith("/")
        or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
    ):
        raise InvalidFile("Use a relative path inside the workspace, without .git or traversal")
    return PurePosixPath(value).as_posix()


@dataclass(frozen=True)
class FileInfo:
    mime_type: str
    preview_kind: str
    extracted_text: str = ""
    truncated: bool = False
    width: int | None = None
    height: int | None = None


def bounded_inspect_file(filename: str, data: bytes) -> FileInfo:
    """Complex document parsers run outside the API/worker process with a deadline."""
    validate_relative_path(filename)
    if len(data) > MAX_FILE_BYTES:
        raise InvalidFile("File exceeds the 25 MiB limit")
    if PurePosixPath(filename).suffix.lower() not in {".pdf", ".docx", ".pptx", ".xlsx"}:
        return inspect_file(filename, data)
    # Keep Windows runtime lookup variables, but no database/model/app secrets.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-m", "jhin_media.file_extract", filename],
            input=data,
            capture_output=True,
            timeout=15,
            env=environment,
            check=False,
        )
        if result.returncode or len(result.stdout) > 2_000_000:
            raise InvalidFile("Document extraction failed or exceeded its time/memory limit")
        payload = json.loads(result.stdout)
        return FileInfo(**payload["result"])
    except (subprocess.TimeoutExpired, ValueError, KeyError, TypeError, OSError) as exc:
        raise InvalidFile("Document could not be extracted within its supported limits") from exc


class FileStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or os.environ.get("JHIN_FILES_ROOT", "/data/files")).resolve()

    def path(self, workspace_id: UUID, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InvalidFile("Invalid blob identifier")
        path = self.root / str(UUID(str(workspace_id))) / digest[:2] / digest
        if (
            path.parent.parent.is_symlink()
            or path.parent.is_symlink()
            or not path.resolve().is_relative_to(self.root)
        ):
            raise InvalidFile("Blob path escapes managed storage")
        return path

    def put(self, workspace_id: UUID, data: bytes) -> str:
        if len(data) > MAX_FILE_BYTES:
            raise InvalidFile("File exceeds the 25 MiB limit")
        digest = hashlib.sha256(data).hexdigest()
        path = self.path(workspace_id, digest)
        with _workspace_blob_lock(path.parent.parent):
            if path.exists():
                if self.read(workspace_id, digest) != data:
                    raise InvalidFile("Managed blob integrity check failed")
                return digest
            self._check_quota(path.parent.parent, len(data))
            self._publish_blob(workspace_id, digest, path, data)
        return digest

    def _check_quota(self, directory: Path, incoming: int) -> None:
        try:
            limit = int(os.environ.get("JHIN_FILES_WORKSPACE_QUOTA_BYTES", str(10 * 1024**3)))
        except ValueError as exc:
            raise InvalidFile("Managed storage quota configuration is invalid") from exc
        if limit < 1:
            raise InvalidFile("Managed storage quota must be positive")
        used, count = 0, 0
        for prefix in directory.iterdir():
            if not re.fullmatch(r"[a-f0-9]{2}", prefix.name):
                continue
            if prefix.is_symlink() or not prefix.is_dir():
                raise InvalidFile("Managed storage layout is invalid")
            for blob in prefix.iterdir():
                if blob.is_symlink() or not blob.is_file():
                    raise InvalidFile("Managed storage layout is invalid")
                used += blob.stat().st_size
                count += 1
                if count >= 100_000 or used + incoming > limit:
                    raise StorageQuotaExceeded(
                        "Managed file storage quota reached; saved versions remain available. "
                        "Increase JHIN_FILES_WORKSPACE_QUOTA_BYTES or export retained files."
                    )
        if used + incoming > limit:
            raise StorageQuotaExceeded(
                "Managed file storage quota reached; saved files are preserved"
            )

    def _publish_blob(self, workspace_id: UUID, digest: str, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish a fully fsynced inode atomically. A second writer must never
        # observe a partially written blob with its final content hash.
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".upload-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or self.read(workspace_id, digest) != data:
                raise InvalidFile("Managed blob integrity check failed") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, workspace_id: UUID, digest: str) -> bytes:
        path = self.path(workspace_id, digest)
        if path.is_symlink():
            raise InvalidFile("Managed blob may not be a symlink")
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != digest:
            raise InvalidFile("Managed blob integrity check failed")
        return data


_TEXT_MIMES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".json": "application/json",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".js": "text/javascript",
    ".jsx": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
}
_CODE_EXTENSIONS = {
    ".py",
    ".rb",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".php",
    ".sh",
    ".bash",
    ".ps1",
    ".sql",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".log",
    ".r",
    ".vue",
    ".svelte",
    ".lock",
    ".gitignore",
    ".env.example",
    ".ipynb",
}
_OFFICE = {
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "document",
        "word/",
    ),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "slides",
        "ppt/slides/",
    ),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "spreadsheet",
        "xl/",
    ),
}


def _xml_text(data: bytes) -> list[str]:
    # ElementTree does not fetch external entities; reject internal DTDs too.
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise InvalidFile("Document XML entities are not supported")
    root = ElementTree.fromstring(data)
    return [
        element.text or ""
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] in {"t", "v"}
    ]


def _xlsx_text(archive: ZipFile) -> str:
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        data = archive.read("xl/sharedStrings.xml")
        _xml_text(data)
        root = ElementTree.fromstring(data)
        shared = ["".join(node.itertext()) for node in root.findall("x:si", namespace)]
    parts: list[str] = []
    length = 0
    for name in sorted(archive.namelist()):
        if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
            continue
        data = archive.read(name)
        _xml_text(data)
        root = ElementTree.fromstring(data)
        parts.append(f"Sheet: {PurePosixPath(name).stem}")
        for cell in root.findall(".//x:sheetData/x:row/x:c", namespace):
            value = cell.findtext("x:v", default="", namespaces=namespace)
            kind = cell.attrib.get("t")
            if kind == "s":
                try:
                    index = int(value)
                    value = shared[index] if 0 <= index < len(shared) else "[invalid shared string]"
                except ValueError:
                    value = "[invalid shared string]"
            elif kind == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(".//x:t", namespace))
            elif kind == "b":
                value = "TRUE" if value == "1" else "FALSE"
            formula = cell.findtext("x:f", namespaces=namespace)
            line = f"{cell.attrib.get('r', '?')}: {value}"
            if formula:
                line += f" [formula: ={formula}]"
            parts.append(line)
            length += len(line)
            if length > MAX_EXTRACTED_CHARS:
                return "\n".join(parts)
    if not parts:
        raise InvalidFile("Spreadsheet has no supported worksheets")
    return "\n".join(parts)


def _office_text(data: bytes, extension: str) -> str:
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            total = sum(member.file_size for member in members)
            if (
                len(members) > MAX_ARCHIVE_MEMBERS
                or total > MAX_ARCHIVE_BYTES
                or total > max(len(data) * 150, 1_000_000)
            ):
                raise InvalidFile("Document archive expansion limit exceeded")
            if "[Content_Types].xml" not in archive.namelist():
                raise InvalidFile("Invalid Office document")
            if any(member.flag_bits & 1 or member.file_size > 10_000_000 for member in members):
                raise InvalidFile("Encrypted or oversized document parts are not supported")
            if extension == ".xlsx":
                return _xlsx_text(archive)
            expected = _OFFICE[extension][2]
            parts: list[str] = []
            length = 0
            for member in sorted(members, key=lambda member: member.filename):
                name = member.filename
                if member.flag_bits & 1:
                    raise InvalidFile("Encrypted Office documents are not supported")
                if not name.startswith(expected) or not name.endswith(".xml"):
                    continue
                if extension == ".docx" and name != "word/document.xml":
                    continue
                if extension == ".xlsx" and not (
                    name.startswith("xl/worksheets/") or name == "xl/sharedStrings.xml"
                ):
                    continue
                if member.file_size > 10_000_000:
                    raise InvalidFile("Document part exceeds extraction limit")
                text = "\n".join(_xml_text(archive.read(member)))
                parts.append(text)
                length += len(text)
                if length > MAX_EXTRACTED_CHARS:
                    break
            if not parts:
                raise InvalidFile("Office document has no supported content")
            return "\n\n".join(parts)
    except (BadZipFile, ElementTree.ParseError, KeyError, RuntimeError) as exc:
        raise InvalidFile("Invalid or unreadable Office document") from exc


def inspect_file(filename: str, data: bytes) -> FileInfo:
    """Inspect bytes, with deterministic bounded extraction and no network IO."""
    validate_relative_path(filename)
    if len(data) > MAX_FILE_BYTES:
        raise InvalidFile("File exceeds the 25 MiB limit")
    extension = PurePosixPath(filename).suffix.lower()
    if extension in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        try:
            with Image.open(io.BytesIO(data)) as picture:
                if picture.width * picture.height > 40_000_000:
                    raise InvalidFile("Image exceeds the 40 megapixel limit")
                width, height = picture.size
                mime = Image.MIME.get(picture.format or "", "")
                picture.verify()
                if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                    raise InvalidFile("Unsupported image format")
                return FileInfo(mime, "image", width=width, height=height)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise InvalidFile("Invalid image data") from exc
    if extension == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise InvalidFile("Invalid PDF data")
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise InvalidFile("Encrypted PDF documents are not supported")
            pages: list[str] = []
            length = 0
            for page in list(reader.pages)[:200]:
                text = page.extract_text() or ""
                pages.append(text)
                length += len(text)
                if length > MAX_EXTRACTED_CHARS:
                    break
            content = "\n\n".join(pages)
            return FileInfo(
                "application/pdf",
                "pdf",
                content[:MAX_EXTRACTED_CHARS],
                len(content) > MAX_EXTRACTED_CHARS or len(reader.pages) > 200,
            )
        except InvalidFile:
            raise
        except ImportError as exc:
            raise InvalidFile("PDF extraction runtime is unavailable") from exc
        except Exception as exc:
            raise InvalidFile("Invalid or unreadable PDF") from exc
    if extension in _OFFICE:
        mime, kind, _ = _OFFICE[extension]
        content = _office_text(data, extension)
        return FileInfo(
            mime, kind, content[:MAX_EXTRACTED_CHARS], len(content) > MAX_EXTRACTED_CHARS
        )
    if (
        extension in _TEXT_MIMES
        or extension in _CODE_EXTENSIONS
        or "." not in PurePosixPath(filename).name
        or filename.endswith(".env.example")
    ):
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InvalidFile("Text files must use UTF-8") from exc
        if "\x00" in content:
            raise InvalidFile("Binary data cannot be uploaded as text")
        kind = (
            "spreadsheet"
            if extension in {".csv", ".tsv"}
            else "text"
            if extension in {".txt", ".md"}
            else "code"
        )
        if extension in {".csv", ".tsv"}:
            try:
                next(
                    csv.reader(
                        io.StringIO(content), delimiter="\t" if extension == ".tsv" else ","
                    ),
                    [],
                )
            except csv.Error as exc:
                raise InvalidFile("Invalid delimited text file") from exc
        return FileInfo(
            _TEXT_MIMES.get(extension, "text/plain"),
            kind,
            content[:MAX_EXTRACTED_CHARS],
            len(content) > MAX_EXTRACTED_CHARS,
        )
    raise InvalidFile("Supported files: text/code, images, PDF, CSV, XLSX, DOCX and PPTX")
