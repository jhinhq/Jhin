"""The closed vocabularies, kept honest across their three copies.

Categories and icons are declared here, in :mod:`jhin_connectors.catalog`, and
again in the web app's icon map. Three copies is a deliberate trade — the sync
must not import the web app, and the web app must not import Python — but
three copies that drift produce entries filed under a category no filter
offers and icons that silently fall back to a generic plug. This test is the
seam that makes drift a failing build instead of a support ticket.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jhin_catalog_sync.vocab import CATALOG_CATEGORIES, CATALOG_ICONS
from jhin_connectors.catalog import CATALOG_CATEGORIES as CONNECTOR_CATEGORIES
from jhin_connectors.catalog import load_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]
LOGO_TILE = REPO_ROOT / "apps" / "web" / "components" / "catalog" / "logo-tile.tsx"


def test_categories_match_the_connector_catalog_element_for_element() -> None:
    assert tuple(CATALOG_CATEGORIES) == tuple(CONNECTOR_CATEGORIES)
    assert len(CATALOG_CATEGORIES) == 12
    # Order is load-bearing: the gallery's category rail renders in this order.
    assert CATALOG_CATEGORIES[0] == "Developer tools"
    assert CATALOG_CATEGORIES[-1] == "Storage"


def test_the_icon_vocabulary_is_closed_and_covers_the_curated_entries() -> None:
    assert len(CATALOG_ICONS) == 33
    assert len(set(CATALOG_ICONS)) == len(CATALOG_ICONS)
    assert "mcp" in CATALOG_ICONS, "the fallback icon for a plain MCP server"

    used = {entry.icon for entry in load_catalog()}
    assert used <= set(CATALOG_ICONS), (
        f"curated icons outside the vocabulary: {used - set(CATALOG_ICONS)}"
    )


def test_every_icon_has_a_renderer_in_the_web_icon_map() -> None:
    """An icon the browser cannot draw is worse than no icon: the card silently
    degrades to a plug and nobody notices which entry lost its identity. The
    map lives in ``LogoTile`` — the shared tile every catalog card renders."""
    source = LOGO_TILE.read_text(encoding="utf-8")
    block = re.search(r"const ICONS: Record<string, LucideIcon> = \{(.*?)\n\};", source, re.S)
    assert block is not None, "the web icon map moved; this cross-check needs updating"
    rendered = set(re.findall(r'^\s+"?([a-z][a-z-]*)"?:', block.group(1), re.M))

    assert set(CATALOG_ICONS) <= rendered, (
        f"icons with no renderer: {set(CATALOG_ICONS) - rendered}"
    )


def test_every_curated_category_is_one_the_catalog_declares() -> None:
    raw = json.loads(
        (
            REPO_ROOT / "packages" / "connectors" / "src" / "jhin_connectors" / "catalog.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["category"] for item in raw} <= set(CATALOG_CATEGORIES)
