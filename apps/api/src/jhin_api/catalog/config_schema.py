"""The Connect dialog's render contract, built server-side from installed manifests.

This module exists to close one hole. A catalog entry is crawled from the open
internet; if its payload were allowed to *describe a form* — field names, types,
enums, secrecy — then whoever published that entry would be writing the dialog
somebody types their credentials into. So the field list here comes only from
the manifest of a connector this install actually has, and catalog data
contributes nothing but values for names that manifest already declared.
Unknown keys are dropped without comment; nothing the catalog says can add a
field, rename one, mark one secret, or widen a bound.

The result is deliberately small and deliberately boring: four field types, a
capped field count, capped enums, a byte ceiling on the whole document. A
renderer that meets something it cannot honour degrades to a text input rather
than refusing to open, and the server says which fields it had to flatten in
``degraded`` so the dialog can be honest about it.
"""

from __future__ import annotations

from collections.abc import Mapping

from jhin_api.catalog.schemas import ConfigSchemaAuth, ConfigSchemaField, ConfigSchemaOut
from jhin_connectors import ConfigFieldSpec, ConnectorManifest, default_registry
from jhin_connectors.mcp.manifest import (
    MCP_CONNECTOR_TYPE,
    TRANSPORT_AUTO,
    TRANSPORT_SSE,
    TRANSPORTS,
)

MAX_FIELDS: int = 24
MAX_ENUM_OPTIONS: int = 20
MAX_ENUM_OPTION_CHARS: int = 200
MAX_LENGTH_CEILING: int = 2_000
MAX_SCHEMA_BYTES: int = 8_192

#: Manifest field kinds → the four types the renderer knows. A kind absent
#: from this table is emitted as ``"string"`` and named in ``degraded``.
_KINDS: dict[str, str] = {
    "text": "string",
    "integer": "integer",
    "boolean": "boolean",
    "string_list": "string_list",
}

#: The longest value each known field will accept, keyed by
#: ``(connector_type, field name)``. Manifests do not carry a length, and a
#: catalog entry must never be allowed to state one, so the few bounds worth
#: showing a person live here — beside the code that enforces them.
_FIELD_MAX_LENGTH: dict[tuple[str, str], int] = {
    (MCP_CONNECTOR_TYPE, "server_url"): 512,
    (MCP_CONNECTOR_TYPE, "server_slug"): 32,
}

#: Closed option lists, for the same reason and from the same place: a set of
#: choices is a promise about what the backend accepts, so it comes from our
#: own constants and never from the index.
_FIELD_ENUM: dict[tuple[str, str], tuple[str, ...]] = {
    (MCP_CONNECTOR_TYPE, "transport"): TRANSPORTS,
}

_AUTH_HINTS = frozenset({"none", "bearer", "header", "oauth"})
_MAX_PREFILL_KEY_CHARS = 64
_MAX_PREFILL_VALUE_CHARS = 500
_TRUNCATED = "__truncated__"


def _manifest_for(connector_type: str | None) -> ConnectorManifest | None:
    """The manifest that will actually serve this entry, or None.

    A native connector when the entry names one this install has; otherwise
    the generic MCP connector. Naming a connector that is not installed falls
    through to MCP rather than failing — the endpoint is usually still
    reachable as a plain MCP server.
    """
    registry = default_registry()
    if connector_type:
        native = registry.get(connector_type)
        if native is not None:
            return native.manifest
    generic = registry.get(MCP_CONNECTOR_TYPE)
    return generic.manifest if generic is not None else None


def _applicable_fields(manifest: ConnectorManifest, auth_hint: str) -> tuple[ConfigFieldSpec, ...]:
    """Manifest config fields that apply to the auth method this entry hints at.

    A field scoped to another auth scheme (the MCP connector's ``header_name``,
    which only exists for custom-header auth) would otherwise render as a
    required control nobody can sensibly fill for a bearer-token server. When
    the hint names no scheme the manifest has, only the unscoped fields survive.
    """
    known = manifest.auth_scheme(auth_hint) is not None
    return tuple(
        field
        for field in manifest.config_fields
        if not field.auth_types or (known and auth_hint in field.auth_types)
    )


def _enum_for(connector_type: str, name: str) -> list[str]:
    """A closed option list, only when it is one a renderer should honour."""
    options = _FIELD_ENUM.get((connector_type, name))
    if options is None:
        return []
    distinct = list(dict.fromkeys(options))
    if not 2 <= len(distinct) <= MAX_ENUM_OPTIONS:
        return []
    if any(not option or len(option) > MAX_ENUM_OPTION_CHARS for option in distinct):
        return []
    return distinct


def _max_length_for(connector_type: str, name: str) -> int | None:
    limit = _FIELD_MAX_LENGTH.get((connector_type, name))
    return limit if limit is not None and 1 <= limit <= MAX_LENGTH_CEILING else None


def _bounds_for(field: ConfigFieldSpec, field_type: str) -> tuple[int | None, int | None]:
    """Integer bounds, and only when both sides are present and consistent."""
    if field_type != "integer" or field.minimum is None or field.maximum is None:
        return None, None
    if field.minimum > field.maximum:
        return None, None
    return field.minimum, field.maximum


def _typed_default(
    value: object, *, field_type: str, options: list[str]
) -> str | int | bool | list[str] | None:
    """A default the renderer can actually put in the control, or nothing.

    A mistyped default is dropped rather than coerced: showing somebody the
    wrong prefilled value is worse than showing them an empty field.
    """
    if field_type == "boolean":
        return value if isinstance(value, bool) else None
    if field_type == "integer":
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if field_type == "string_list":
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        return None
    if not isinstance(value, str):
        return None
    if options and value not in options:
        return None
    return value


def _mcp_defaults(
    *, slug: str, mcp_url: str | None, url_unverified: bool, transport: str
) -> dict[str, str]:
    """What the index knows about *this* server, mapped onto the MCP form.

    An unverified or non-https endpoint contributes an empty string, never a
    guess: the dialog then asks for the URL from the provider's own docs,
    which is the only place it is trustworthy.
    """
    usable_url = (
        mcp_url if mcp_url and mcp_url.startswith("https://") and not url_unverified else ""
    )
    return {
        "server_slug": slug,
        "server_url": usable_url,
        "transport": TRANSPORT_SSE if transport == TRANSPORT_SSE else TRANSPORT_AUTO,
    }


def _prefill(
    connector_config: Mapping[str, str], *, names: frozenset[str], secret_names: frozenset[str]
) -> dict[str, str]:
    """Catalog-supplied values for manifest fields, filtered to the safe ones."""
    values: dict[str, str] = {}
    for key, value in connector_config.items():
        # No isinstance guard: the two callers both build this mapping through
        # ``clean_text``, so it is str -> str by construction rather than by
        # inspection here.
        if key not in names or key in secret_names:
            continue
        if len(key) > _MAX_PREFILL_KEY_CHARS or len(value) > _MAX_PREFILL_VALUE_CHARS:
            continue
        values[key] = value
    return values


def build_config_schema(
    *,
    connector_type: str | None,
    slug: str,
    mcp_url: str | None,
    url_unverified: bool,
    transport: str,
    auth_hint: str,
    auth_note: str,
    connector_config: Mapping[str, str],
) -> ConfigSchemaOut | None:
    """Build the render contract for one catalog entry.

    The field LIST comes only from the installed connector manifest — the
    native connector when ``connector_type`` names one that is installed,
    otherwise the ``mcp`` connector; None when neither is installed. Catalog
    data never contributes a field definition, only a default value.

    Defaults applied, in this order and no other:

    * every manifest field's own ``default``;
    * for the mcp connector: ``server_slug`` = slug, ``server_url`` = mcp_url
      when it is https and not ``url_unverified`` else "", ``transport`` =
      "sse" when transport == "sse" else "auto";
    * ``connector_config[k]`` for each k that names an existing field, is
      <= 64 chars, and whose value is <= 500 chars. Unknown keys are dropped
      silently. A value is never applied to a field whose manifest auth scheme
      marks it secret.

    A manifest field whose ``kind`` is not in ``_KINDS`` is emitted as
    ``type="string"`` and its name is appended to ``degraded``. ``enum`` is
    emitted only for 2..MAX_ENUM_OPTIONS distinct strings each
    <= MAX_ENUM_OPTION_CHARS. ``max_length`` only when 1..MAX_LENGTH_CEILING.
    ``minimum``/``maximum`` only for integer fields with both present and
    min <= max. ``secret`` is always False today (credentials stay on the
    manifest auth-scheme path) but the field is part of the contract.

    Fields beyond MAX_FIELDS are dropped. If the serialised result exceeds
    MAX_SCHEMA_BYTES the function returns a schema truncated to the required
    fields only, with "__truncated__" appended to ``degraded``.
    """
    manifest = _manifest_for(connector_type)
    if manifest is None:
        return None

    resolved_type = manifest.connector_type
    hint = auth_hint if auth_hint in _AUTH_HINTS else "bearer"
    specs = _applicable_fields(manifest, hint)[:MAX_FIELDS]
    names = frozenset(spec.name for spec in specs)
    secret_names = frozenset(
        secret.name for scheme in manifest.auth_schemes for secret in scheme.secret_fields
    )

    overrides: dict[str, str] = {}
    if resolved_type == MCP_CONNECTOR_TYPE:
        overrides.update(
            {
                name: value
                for name, value in _mcp_defaults(
                    slug=slug,
                    mcp_url=mcp_url,
                    url_unverified=url_unverified,
                    transport=transport,
                ).items()
                if name in names
            }
        )
    overrides.update(_prefill(connector_config, names=names, secret_names=secret_names))

    degraded: list[str] = []
    fields: list[ConfigSchemaField] = []
    for spec in specs:
        field_type = _KINDS.get(spec.kind)
        if field_type is None:
            field_type = "string"
            degraded.append(spec.name)
        options = _enum_for(resolved_type, spec.name)
        minimum, maximum = _bounds_for(spec, field_type)
        raw_default: object | None = overrides.get(spec.name, spec.default)
        fields.append(
            ConfigSchemaField(
                name=spec.name,
                label=spec.label,
                type=field_type,
                required=spec.required,
                secret=False,
                default=_typed_default(raw_default, field_type=field_type, options=options),
                enum=options,
                max_length=_max_length_for(resolved_type, spec.name),
                minimum=minimum,
                maximum=maximum,
                placeholder=spec.placeholder,
                help=spec.help,
                multiline=False,
            )
        )

    schema = ConfigSchemaOut(
        connector_type=resolved_type,
        fields=fields,
        auth=ConfigSchemaAuth(type=hint, note=auth_note),
        degraded=degraded,
    )
    if len(schema.model_dump_json().encode("utf-8")) <= MAX_SCHEMA_BYTES:
        return schema
    # Over the ceiling: keep what the person cannot submit the form without,
    # and say plainly that the rest was dropped.
    return schema.model_copy(
        update={
            "fields": [field for field in fields if field.required],
            "degraded": [*degraded, _TRUNCATED],
        }
    )
