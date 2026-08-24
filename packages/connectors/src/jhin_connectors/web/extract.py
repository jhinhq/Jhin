"""Crude-but-safe readable-text extraction (stdlib ``html.parser`` only).

The goal is a bounded plain-text projection of a page for a model to read:
scripts, styles, and other non-content subtrees are dropped, block elements
become line breaks, and the result is hard-capped. This is intentionally not
a fidelity-preserving renderer.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from jhin_connectors.web.schemas import MAX_PAGE_TEXT_CHARS, MAX_TITLE_CHARS

# Subtrees that never contribute readable text.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "object"})
# Elements that imply a line break around their content.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "br",
        "tr",
        "table",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_SPACES_RE = re.compile(r"[ \t\r\f\v]+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if data.strip():
            self.parts.append(data)


def _normalize(text: str) -> str:
    collapsed = _SPACES_RE.sub(" ", text)
    lines = "\n".join(line.strip() for line in collapsed.split("\n"))
    return _BLANK_LINES_RE.sub("\n\n", lines).strip()


def extract_readable_text(html: str) -> tuple[str, str, bool]:
    """``(title, text, truncated)`` from one HTML document.

    Never raises on malformed markup — ``html.parser`` is tolerant, and the
    fallback for a page it cannot make sense of is simply less text.
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    title = _normalize(" ".join(parser.title_parts))[:MAX_TITLE_CHARS]
    text = _normalize(" ".join(parser.parts))
    truncated = len(text) > MAX_PAGE_TEXT_CHARS
    return title, text[:MAX_PAGE_TEXT_CHARS], truncated


def clip_plain_text(text: str) -> tuple[str, bool]:
    """Bound non-HTML textual bodies (plain text, JSON, XML) the same way."""
    normalized = text.strip()
    truncated = len(normalized) > MAX_PAGE_TEXT_CHARS
    return normalized[:MAX_PAGE_TEXT_CHARS], truncated


__all__ = ["clip_plain_text", "extract_readable_text"]
