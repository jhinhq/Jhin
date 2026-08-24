"""Pure backend adapters, URL/domain policy, and text extraction."""

from __future__ import annotations

import pytest

from jhin_connectors.endpoints import EndpointPolicyError
from jhin_connectors.web.client import (
    backend_base_url,
    build_search_request,
    domain_allowed,
    parse_search_results,
    validate_allowed_domains,
    validate_backend,
    validate_fetch_url,
)
from jhin_connectors.web.extract import clip_plain_text, extract_readable_text
from jhin_connectors.web.schemas import MAX_PAGE_TEXT_CHARS, MAX_SNIPPET_CHARS, WebFetchInput


def test_backend_validation_and_default_base_urls() -> None:
    assert validate_backend(" Tavily ") == "tavily"
    with pytest.raises(ValueError, match="search_backend"):
        validate_backend("google")
    assert backend_base_url("tavily", {}) == "https://api.tavily.com"
    assert backend_base_url("brave", {}) == "https://api.search.brave.com"
    assert backend_base_url("exa", {}) == "https://api.exa.ai"


def test_base_url_override_needs_policy_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(EndpointPolicyError):
        backend_base_url("tavily", {"base_url": "http://fake-websearch:8080"})
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", "http://fake-websearch:8080")
    assert backend_base_url("tavily", {"base_url": "http://fake-websearch:8080"}) == (
        "http://fake-websearch:8080"
    )


def test_search_request_shapes_match_each_provider() -> None:
    tavily = build_search_request("tavily", "https://api.tavily.com", "tok", "jhin", 3)
    assert (tavily.method, tavily.url) == ("POST", "https://api.tavily.com/search")
    assert tavily.headers["Authorization"] == "Bearer tok"
    assert tavily.json_body == {"query": "jhin", "max_results": 3}

    brave = build_search_request("brave", "https://api.search.brave.com", "tok", "jhin", 3)
    assert (brave.method, brave.url) == ("GET", "https://api.search.brave.com/res/v1/web/search")
    assert brave.headers["X-Subscription-Token"] == "tok"
    assert brave.params == {"q": "jhin", "count": "3"}
    assert brave.json_body is None

    exa = build_search_request("exa", "https://api.exa.ai", "tok", "jhin", 3)
    assert (exa.method, exa.url) == ("POST", "https://api.exa.ai/search")
    assert exa.headers["x-api-key"] == "tok"
    assert exa.json_body == {"query": "jhin", "numResults": 3}


def test_parse_normalizes_all_three_shapes_and_bounds_output() -> None:
    tavily = parse_search_results(
        "tavily",
        {
            "results": [
                {
                    "title": "T",
                    "url": "https://a.example/",
                    "content": "c" * 2_000,
                    "published_date": "2026-01-01",
                },
                {"url": "ftp://nope.example/"},
                "junk",
            ]
        },
        5,
    )
    assert len(tavily) == 1
    assert tavily[0].published == "2026-01-01"
    assert len(tavily[0].snippet) == MAX_SNIPPET_CHARS

    brave = parse_search_results(
        "brave",
        {
            "web": {
                "results": [
                    {"title": "B", "url": "https://b.example/", "description": "d", "page_age": "x"}
                ]
            }
        },
        5,
    )
    assert brave[0].snippet == "d"
    assert brave[0].published == "x"

    exa = parse_search_results(
        "exa",
        {
            "results": [
                {"title": "E", "url": "https://e.example/", "text": "t", "publishedDate": "y"}
            ]
        },
        5,
    )
    assert exa[0].snippet == "t"
    assert exa[0].published == "y"

    many = parse_search_results(
        "tavily",
        {"results": [{"title": "n", "url": f"https://x.example/{i}"} for i in range(20)]},
        4,
    )
    assert len(many) == 4
    assert parse_search_results("tavily", "garbage", 5) == []


def test_allowed_domains_validation_and_matching() -> None:
    assert validate_allowed_domains(None) == []
    assert validate_allowed_domains(["Docs.Python.org", "*.wikipedia.org"]) == [
        "docs.python.org",
        "*.wikipedia.org",
    ]
    with pytest.raises(ValueError, match="host patterns"):
        validate_allowed_domains(["bad pattern with spaces"])
    with pytest.raises(ValueError, match="at most"):
        validate_allowed_domains([f"h{i}.example" for i in range(60)])

    patterns = ["docs.python.org", "*.wikipedia.org"]
    assert domain_allowed("docs.python.org", patterns)
    assert domain_allowed("en.wikipedia.org", patterns)
    assert not domain_allowed("evil.example", patterns)
    assert domain_allowed("anything.example", [])


def test_fetch_url_policy_rejects_private_and_out_of_domain_targets() -> None:
    assert validate_fetch_url("https://docs.python.org/3/", []).startswith("https://")
    with pytest.raises(EndpointPolicyError):
        validate_fetch_url("http://localhost:8080/", [])
    with pytest.raises(EndpointPolicyError, match="allowed domains"):
        validate_fetch_url("https://evil.example/", ["docs.python.org"])


def test_fetch_input_derives_the_domain_scope_from_the_url() -> None:
    data = WebFetchInput(
        connection_id="c", url="https://Docs.Python.org/3/library/", domain="forged.example"
    )
    assert data.domain == "docs.python.org"
    assert WebFetchInput(connection_id="c", url="not a url").domain == ""


def test_extraction_strips_scripts_and_bounds_text() -> None:
    html = (
        "<html><head><title>My Page</title><style>b{}</style>"
        "<script>alert('never')</script></head>"
        "<body><h1>Heading</h1><p>First paragraph.</p><noscript>hidden</noscript>"
        "<ul><li>item one</li><li>item two</li></ul></body></html>"
    )
    title, text, truncated = extract_readable_text(html)
    assert title == "My Page"
    assert "First paragraph." in text
    assert "item one" in text
    assert "alert" not in text
    assert "hidden" not in text
    assert truncated is False

    _title, huge_text, huge_truncated = extract_readable_text("<p>" + ("word " * 30_000) + "</p>")
    assert huge_truncated is True
    assert len(huge_text) == MAX_PAGE_TEXT_CHARS

    clipped, was_clipped = clip_plain_text("x" * (MAX_PAGE_TEXT_CHARS + 10))
    assert was_clipped is True
    assert len(clipped) == MAX_PAGE_TEXT_CHARS
