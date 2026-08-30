"""The Connect dialog's render contract, and the hole it exists to close.

A catalog entry is crawled from the open internet. If its payload could
*describe a form* — field names, types, enums, which box is a password — then
whoever published that entry would be writing the dialog somebody types a
credential into. So every test here is a variation on one assertion: the field
list comes from an installed manifest, and catalog data contributes values for
names that manifest already declared, or nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from jhin_api.catalog import config_schema as module
from jhin_api.catalog.config_schema import (
    MAX_ENUM_OPTIONS,
    MAX_FIELDS,
    MAX_SCHEMA_BYTES,
    build_config_schema,
)
from jhin_api.catalog.schemas import ConfigSchemaOut

MCP_ENTRY: dict[str, Any] = {
    "connector_type": None,
    "slug": "kestrel",
    "mcp_url": "https://mcp.example.com/kestrel",
    "url_unverified": False,
    "transport": "streamable_http",
    "auth_hint": "bearer",
    "auth_note": "Create a token in Kestrel settings.",
    "connector_config": {},
}


def _schema(**overrides: Any) -> ConfigSchemaOut:
    built = build_config_schema(**{**MCP_ENTRY, **overrides})
    assert built is not None
    return built


def _by_name(schema: ConfigSchemaOut) -> dict[str, Any]:
    return {item.name: item for item in schema.fields}


# --------------------------------------------------------------------------
# the generic MCP form
# --------------------------------------------------------------------------


def test_the_mcp_form_is_the_three_fields_with_the_entry_filled_in() -> None:
    schema = _schema()
    fields = _by_name(schema)

    assert schema.version == 1
    assert schema.connector_type == "mcp"
    assert set(fields) == {"server_url", "server_slug", "transport"}
    assert fields["server_slug"].default == "kestrel"
    assert fields["server_url"].default == "https://mcp.example.com/kestrel"
    assert fields["transport"].default == "auto"
    assert fields["server_url"].required is True
    assert fields["transport"].required is False
    assert schema.auth.type == "bearer"
    assert schema.auth.note == "Create a token in Kestrel settings."
    assert schema.degraded == []


@pytest.mark.parametrize(
    ("url", "unverified"),
    [
        ("https://mcp.example.com/kestrel", True),
        ("http://mcp.example.com/kestrel", False),
        (None, False),
        ("", False),
    ],
    ids=["unverified", "plain-http", "missing", "empty"],
)
def test_an_endpoint_we_cannot_stand_behind_is_left_blank(
    url: str | None, unverified: bool
) -> None:
    """A guessed URL in a prefilled box is a guess somebody will trust. Empty
    sends them to the provider's own docs, which is the only place it is true."""
    fields = _by_name(_schema(mcp_url=url, url_unverified=unverified))

    assert fields["server_url"].default == ""
    assert fields["server_slug"].default == "kestrel"


def test_the_transport_default_follows_the_entry_and_offers_the_closed_set() -> None:
    assert _by_name(_schema(transport="sse"))["transport"].default == "sse"
    assert _by_name(_schema(transport="unknown"))["transport"].default == "auto"

    transport = _by_name(_schema())["transport"]
    assert transport.enum == ["auto", "streamable_http", "sse"]
    assert transport.default in transport.enum


def test_the_custom_header_field_appears_only_for_header_auth() -> None:
    """A required box nobody can sensibly fill is worse than a missing one."""
    assert "header_name" not in _by_name(_schema(auth_hint="bearer"))
    assert "header_name" in _by_name(_schema(auth_hint="header"))


def test_an_auth_hint_outside_the_vocabulary_falls_back_to_bearer() -> None:
    assert _schema(auth_hint="kerberos").auth.type == "bearer"
    assert _schema(auth_hint="oauth").auth.type == "oauth"


def test_a_native_connector_is_used_when_this_install_has_one() -> None:
    schema = _schema(connector_type="github")

    assert schema.connector_type == "github"
    assert "server_url" not in _by_name(schema), "the MCP form is not the GitHub form"


def test_naming_a_connector_this_install_lacks_falls_through_to_mcp() -> None:
    """Usually still reachable as a plain MCP server, so falling back beats
    refusing to render anything."""
    schema = _schema(connector_type="not_installed_anywhere")

    assert schema.connector_type == "mcp"
    assert _by_name(schema)["server_slug"].default == "kestrel"


# --------------------------------------------------------------------------
# what the catalog is allowed to contribute
# --------------------------------------------------------------------------


def test_a_prefill_for_a_known_field_is_applied() -> None:
    fields = _by_name(_schema(connector_config={"server_slug": "renamed"}))

    assert fields["server_slug"].default == "renamed"


def test_a_prefill_for_a_field_the_manifest_never_declared_is_dropped() -> None:
    """The entire injection surface, in one assertion: unknown keys vanish
    without becoming fields, labels, or errors."""
    schema = _schema(
        connector_config={
            "admin_token": "sk-live-please-type-here",
            "callback_url": "https://evil.example.com/collect",
            "server_slug": "kestrel",
        }
    )

    assert set(_by_name(schema)) == {"server_url", "server_slug", "transport"}
    assert schema.degraded == [], "a dropped unknown key is not a degradation, it is a non-event"


def test_an_oversized_prefill_value_or_key_is_dropped() -> None:
    fields = _by_name(
        _schema(
            connector_config={
                "server_slug": "a" * 600,
                "s" * 200: "value",
                "server_url": "https://mcp.example.com/short",
            }
        )
    )

    assert fields["server_slug"].default == "kestrel", "the manifest default stands"
    assert fields["server_url"].default == "https://mcp.example.com/short"


def test_a_prefill_never_lands_on_a_credential_field() -> None:
    """Secret fields come off the manifest's auth scheme, not the form, and a
    prefilled password box is a phishing box."""
    schema = _schema(connector_config={"token": "sk-live-not-yours"})

    assert "token" not in _by_name(schema)
    assert all(item.secret is False for item in schema.fields)
    rendered = schema.model_dump_json()
    assert "sk-live-not-yours" not in rendered


def test_a_prefill_of_the_wrong_type_is_dropped_rather_than_coerced() -> None:
    """Showing somebody the wrong prefilled value is worse than an empty box."""
    fields = _by_name(_schema(connector_config={"transport": "carrier-pigeon"}))

    assert fields["transport"].default in (None, "auto")
    assert fields["transport"].default != "carrier-pigeon"


def test_no_field_is_ever_marked_secret() -> None:
    for entry in (
        MCP_ENTRY,
        {**MCP_ENTRY, "auth_hint": "header"},
        {**MCP_ENTRY, "connector_type": "github"},
    ):
        built = build_config_schema(**entry)
        assert built is not None
        assert all(item.secret is False for item in built.fields)


# --------------------------------------------------------------------------
# degradation and ceilings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Spec:
    """A manifest field shaped like ``ConfigFieldSpec`` but free to be wrong —
    the real type's ``kind`` is a closed literal, so the degradation path can
    only be reached from a manifest a future release might ship."""

    name: str
    label: str
    kind: str = "text"
    required: bool = False
    placeholder: str = ""
    help: str = ""
    auth_types: tuple[str, ...] = ()
    default: Any | None = None
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class _Manifest:
    connector_type: str
    config_fields: tuple[_Spec, ...]
    auth_schemes: tuple[Any, ...] = field(default_factory=tuple)

    def auth_scheme(self, _auth_type: str) -> None:
        return None


def _install(monkeypatch: pytest.MonkeyPatch, manifest: _Manifest) -> None:
    monkeypatch.setattr(module, "_manifest_for", lambda _connector_type: manifest)


def test_an_unknown_field_kind_renders_as_text_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _Manifest(
            connector_type="future",
            config_fields=(
                _Spec(name="normal", label="Normal", kind="text"),
                _Spec(name="exotic", label="Exotic", kind="rich_markdown"),
            ),
        ),
    )

    schema = _schema()
    fields = _by_name(schema)

    assert fields["exotic"].type == "string"
    assert schema.degraded == ["exotic"]
    assert fields["normal"].type == "string"


def test_an_enum_longer_than_the_ceiling_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 30-option select is not a select; it is a list nobody reads. The field
    still renders — as a plain text input."""
    monkeypatch.setitem(
        module._FIELD_ENUM,
        ("mcp", "transport"),
        tuple(f"option_{index}" for index in range(MAX_ENUM_OPTIONS + 10)),
    )

    transport = _by_name(_schema())["transport"]

    assert transport.enum == []
    assert transport.type == "string"


def test_a_one_option_enum_is_dropped_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(module._FIELD_ENUM, ("mcp", "transport"), ("auto",))

    assert _by_name(_schema())["transport"].enum == []


def test_integer_bounds_are_emitted_only_when_both_sides_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _Manifest(
            connector_type="bounded",
            config_fields=(
                _Spec(name="port", label="Port", kind="integer", minimum=1, maximum=65_535),
                _Spec(name="halfbound", label="Half", kind="integer", minimum=5),
                _Spec(name="inverted", label="Inverted", kind="integer", minimum=9, maximum=2),
                _Spec(name="texty", label="Texty", kind="text", minimum=1, maximum=10),
            ),
        ),
    )

    fields = _by_name(_schema())

    assert (fields["port"].minimum, fields["port"].maximum) == (1, 65_535)
    assert (fields["halfbound"].minimum, fields["halfbound"].maximum) == (None, None)
    assert (fields["inverted"].minimum, fields["inverted"].maximum) == (None, None)
    assert (fields["texty"].minimum, fields["texty"].maximum) == (None, None)


def test_more_fields_than_the_ceiling_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        _Manifest(
            connector_type="wide",
            config_fields=tuple(
                _Spec(name=f"field_{index}", label=f"Field {index}")
                for index in range(MAX_FIELDS + 12)
            ),
        ),
    )

    assert len(_schema().fields) == MAX_FIELDS


def test_an_oversized_schema_falls_back_to_the_fields_you_cannot_submit_without(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _Manifest(
            connector_type="verbose",
            config_fields=(
                _Spec(name="needed", label="Needed", required=True, help="h" * 40),
                *(
                    _Spec(name=f"chatty_{index}", label=f"Chatty {index}", help="e" * 900)
                    for index in range(MAX_FIELDS - 1)
                ),
            ),
        ),
    )

    schema = _schema()

    assert [item.name for item in schema.fields] == ["needed"]
    assert schema.degraded[-1] == "__truncated__"


def test_the_serialised_schema_stays_under_the_byte_ceiling() -> None:
    for entry in (
        MCP_ENTRY,
        {**MCP_ENTRY, "auth_hint": "header"},
        {**MCP_ENTRY, "connector_type": "github"},
        {**MCP_ENTRY, "auth_note": "n" * 5_000},
        {**MCP_ENTRY, "connector_config": {f"k{index}": "v" * 500 for index in range(50)}},
    ):
        built = build_config_schema(**entry)
        assert built is not None
        assert len(built.model_dump_json().encode("utf-8")) <= MAX_SCHEMA_BYTES


def test_no_installed_connector_at_all_yields_no_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dialog then falls back to its manifest-driven form, which is what it
    did before the catalog existed."""
    monkeypatch.setattr(module, "_manifest_for", lambda _connector_type: None)

    assert build_config_schema(**MCP_ENTRY) is None
