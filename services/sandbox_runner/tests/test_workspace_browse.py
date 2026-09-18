"""The browser lists bounded metadata through directory descriptors on Linux."""

import os
from pathlib import Path

import pytest

from jhin_sandbox_runner.workspace_operations import WorkspaceOperation
from jhin_sandbox_runner.workspace_script import execute


def test_browse_is_a_recognized_read_operation() -> None:
    assert WorkspaceOperation(operation="browse", args={"path": "src"}).operation == "browse"


@pytest.mark.skipif(os.name != "posix", reason="Sandbox directory descriptors run on Linux")
def test_browse_lists_one_level_without_reading_files_or_following_links(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')")
    (tmp_path / "report.txt").write_text("report")
    (tmp_path / ".env.local").write_text("not published")
    (tmp_path / ".git").mkdir()
    (tmp_path / "outside").symlink_to(tmp_path.parent, target_is_directory=True)
    result = execute({"operation": "browse"}, str(tmp_path))
    assert [(item["path"], item["kind"]) for item in result["items"]] == [
        ("src", "directory"),
        ("report.txt", "file"),
    ]
    assert result["omitted"] == 3 and result["truncated"] is False
    nested = execute({"operation": "browse", "args": {"path": "src"}}, str(tmp_path))
    assert nested["items"][0]["path"] == "src/main.py"
    assert "content_base64" not in nested["items"][0]
    for path in ("../", "/etc", ".git", "outside", "src/../../"):
        with pytest.raises((OSError, ValueError)):
            execute({"operation": "browse", "args": {"path": path}}, str(tmp_path))


@pytest.mark.skipif(os.name != "posix", reason="Sandbox directory descriptors run on Linux")
def test_browse_bounds_large_directories(tmp_path: Path) -> None:
    for index in range(270):
        (tmp_path / f"file-{index}").touch()
    result = execute({"operation": "browse"}, str(tmp_path))
    assert len(result["items"]) == 256 and result["truncated"] is True
