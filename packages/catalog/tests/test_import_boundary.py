"""The catalog never reaches agent context.

Everything in ``catalog_entry`` is text somebody else wrote — a README scraped
off a repository nobody at Jhin has read. It is bounded and sanitised at
ingest, but bounded hostile text is still hostile text, and the one reliable
defence against prompt injection is that the model never sees it. So the rule
is structural rather than editorial: the agent packages, the tool packages, and
every worker are forbidden from importing the sync package or the catalog
models at all. The only catalog-derived text an agent ever encounters is what a
*connected* MCP server reports about its own tools, which
``jhin_connectors.mcp.discovery`` already bounds.

If this test fails, the fix is never to add an exception here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Source trees that end up inside a model's context window, directly or one
# hop away: prompts, tool definitions, and the workers that assemble them.
AGENT_FACING_TREES: tuple[Path, ...] = (
    REPO_ROOT / "packages" / "agents" / "src",
    REPO_ROOT / "packages" / "tools" / "src",
    *sorted((REPO_ROOT / "services").glob("*/src")),
)

# The module and the two models. Deliberately not the bare table name: the
# tool worker has a local ``catalog_entry`` variable for the *tool* catalog,
# and a check that cries wolf is a check somebody eventually deletes.
FORBIDDEN = ("jhin_catalog_sync", "CatalogEntry", "CatalogVersion")


def _python_files(tree: Path) -> list[Path]:
    return sorted(path for path in tree.rglob("*.py") if "__pycache__" not in path.parts)


def test_the_trees_under_test_actually_exist() -> None:
    """A path typo would turn this whole file into a test that always passes."""
    assert len(AGENT_FACING_TREES) >= 7
    for tree in AGENT_FACING_TREES:
        assert tree.is_dir(), tree
        assert _python_files(tree), tree


@pytest.mark.parametrize("tree", AGENT_FACING_TREES, ids=lambda path: path.parent.name)
def test_no_agent_facing_source_mentions_the_catalog(tree: Path) -> None:
    pattern = re.compile("|".join(re.escape(token) for token in FORBIDDEN))
    hits: list[str] = []
    for path in _python_files(tree):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")

    assert hits == [], (
        "The catalog carries unreviewed third-party text and must never reach a "
        "prompt or a tool definition:\n" + "\n".join(hits)
    )
