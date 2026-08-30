"""The closed vocabularies a synced entry may be filed under.

Categories and icons are declared here, in :mod:`jhin_connectors.catalog`, and
again in the web app's icon map. Three copies is a deliberate trade — the sync
must not import the web app, and the web app must not import Python, and the
sync deliberately does not depend on ``jhin_connectors`` either — but three
copies that drift produce entries filed under a category no filter offers and
icons that silently fall back to a generic plug. ``tests/test_vocab.py`` is
the seam that makes that drift a failing build instead of a support ticket.

Neither vocabulary is enforced at the wire model. An entry naming a category
or an icon this build does not know is still indexed and still searchable; it
is simply not *publishable* (:func:`jhin_catalog_sync.wire.is_publishable`),
so it never claims a card in the gallery it cannot render.
"""

from __future__ import annotations

# Order is load-bearing: the gallery's category rail renders in this order.
CATALOG_CATEGORIES: tuple[str, ...] = (
    "Developer tools",
    "Project management",
    "Communication",
    "Documents & knowledge",
    "Payments & commerce",
    "CRM & support",
    "Design",
    "Search & web",
    "Data & infrastructure",
    "Automation",
    "Productivity",
    "Storage",
)

# Every icon the web app can actually draw. "mcp" is the fallback for a plain
# indexed server with no brand of its own.
CATALOG_ICONS: tuple[str, ...] = (
    "book-open",
    "bug",
    "calendar",
    "check-square",
    "cloud",
    "cpu",
    "credit-card",
    "database",
    "flame",
    "flask",
    "folder",
    "github",
    "globe",
    "hard-drive",
    "kanban",
    "life-buoy",
    "linear",
    "mail",
    "mcp",
    "message-circle",
    "message-square",
    "notebook",
    "palette",
    "pen-tool",
    "phone",
    "search",
    "send",
    "table",
    "terminal",
    "users",
    "vercel",
    "web",
    "zap",
)

DEFAULT_ICON: str = "mcp"

__all__ = ["CATALOG_CATEGORIES", "CATALOG_ICONS", "DEFAULT_ICON"]
