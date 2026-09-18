"""Managed files are immutable, bounded, and never escape their volume."""

from io import BytesIO
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from jhin_media.files import (
    FileStore,
    InvalidFile,
    bounded_inspect_file,
    inspect_file,
    validate_relative_path,
)


def test_content_addressed_store_is_workspace_scoped_and_immutable(tmp_path):
    store = FileStore(tmp_path)
    workspace, other = uuid4(), uuid4()
    digest = store.put(workspace, b"hello")
    assert store.read(workspace, digest) == b"hello"
    assert store.put(workspace, b"hello") == digest
    with pytest.raises(FileNotFoundError):
        store.read(other, digest)
    with pytest.raises(InvalidFile):
        store.read(workspace, "../secrets")


@pytest.mark.parametrize(
    "name", ["../foo", "/etc/passwd", "C:/secret", "a/../b", "a\\b", "a\x00b", ".git/config"]
)
def test_workspace_paths_are_contained(name):
    with pytest.raises(InvalidFile):
        validate_relative_path(name)


def test_text_content_is_bounded_and_html_is_not_trusted():
    info = inspect_file("index.html", b"<script>alert(1)</script>")
    assert info.mime_type == "text/html"
    assert info.preview_kind == "code"
    assert info.extracted_text == "<script>alert(1)</script>"
    with pytest.raises(InvalidFile):
        inspect_file("photo.png", b"this is not an image")
    with pytest.raises(InvalidFile):
        inspect_file("data.bin", b"\x00\xff")


def test_docx_extraction_reads_only_document_xml():
    data = BytesIO()
    with ZipFile(data, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:p><w:r><w:t>Hello document</w:t></w:r></w:p></w:document>",
        )
    info = inspect_file("report.docx", data.getvalue())
    assert info.preview_kind == "document"
    assert "Hello document" in info.extracted_text
    isolated = bounded_inspect_file("report.docx", data.getvalue())
    assert isolated == info


def test_archive_expansion_is_bounded():
    data = BytesIO()
    with ZipFile(data, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * 2_000_000)
    with pytest.raises(InvalidFile, match="expansion"):
        inspect_file("bomb.docx", data.getvalue())


def test_xlsx_shared_strings_are_resolved_to_cells():
    data = BytesIO()
    with ZipFile(data, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Revenue</t></si></sst>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c>'
            "</row></sheetData></worksheet>",
        )
    info = inspect_file("report.xlsx", data.getvalue())
    assert "A1: Revenue" in info.extracted_text
    assert "B1: 42" in info.extracted_text


def test_pdf_is_inspected_in_bounded_process():
    from pypdf import PdfWriter

    data = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(data)
    info = bounded_inspect_file("report.pdf", data.getvalue())
    assert info.mime_type == "application/pdf"
    assert info.preview_kind == "pdf"
    with pytest.raises(InvalidFile):
        bounded_inspect_file("broken.pdf", b"%PDF-not-a-document")


def test_workspace_blob_quota_deduplicates_and_preserves_retained_bytes(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from jhin_media.files import StorageQuotaExceeded

    monkeypatch.setenv("JHIN_FILES_WORKSPACE_QUOTA_BYTES", "5")
    workspace = uuid4()
    store = FileStore(tmp_path)

    def write(data):
        try:
            return store.put(workspace, data)
        except StorageQuotaExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [b"first", b"other"]))
    assert sum(value is not None for value in results) == 1
    digest = next(value for value in results if value is not None)
    retained = store.read(workspace, digest)
    assert store.put(workspace, retained) == digest
    with pytest.raises(StorageQuotaExceeded):
        store.put(workspace, b"sixsix")
    assert FileStore(tmp_path).read(workspace, digest) == retained
    # Another workspace has its own limit; quota applies to physical blobs,
    # including archived/project references without a live sandbox or DB row.
    assert store.put(uuid4(), b"first")
