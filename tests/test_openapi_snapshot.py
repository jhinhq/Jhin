"""The /api/v1 compatibility gate.

Two things are tested here. First, that the committed snapshot in
``docs/api/openapi.v1.json`` still describes the app this working tree builds —
that is the check which actually fires on a pull request. Second, that the
diff behind it has real semantics: additive change passes, and each shape of
break in ``docs/architecture/api-versioning.md`` fails. The second half is what
makes the first half worth trusting; a detector that only compared documents
byte for byte would fail on every new endpoint and be switched off within a
week.
"""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

snapshot = importlib.import_module("scripts.openapi_snapshot")


# --------------------------------------------------------------------------
# Crafted pairs
# --------------------------------------------------------------------------


def document(
    *,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    response: dict[str, Any] | None = None,
    parameters: list[dict[str, Any]] | None = None,
    security: list[dict[str, list[str]]] | None = None,
    operation_id: str = "list_things",
    extra_paths: dict[str, Any] | None = None,
    responses: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A one-operation OpenAPI document with the knobs each test turns."""
    body_schema = {
        "type": "object",
        "properties": properties if properties is not None else {"name": {"type": "string"}},
        "required": required if required is not None else ["name"],
    }
    response_schema = response if response is not None else {"$ref": "#/components/schemas/Thing"}
    operation: dict[str, Any] = {
        "operationId": operation_id,
        "requestBody": {"content": {"application/json": {"schema": body_schema}}},
        "responses": responses
        if responses is not None
        else {"200": {"content": {"application/json": {"schema": response_schema}}}},
    }
    if parameters is not None:
        operation["parameters"] = parameters
    if security is not None:
        operation["security"] = security
    paths: dict[str, Any] = {"/api/v1/things": {"post": operation}}
    if extra_paths:
        paths.update(extra_paths)
    return {
        "openapi": "3.1.0",
        "info": {"title": "Jhin", "version": "0.1.0"},
        "paths": paths,
        "components": {
            "schemas": {
                "Thing": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "label": {"type": "string"}},
                    "required": ["id", "label"],
                }
            }
        },
    }


def breaks(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    return [change.render() for change in snapshot.breaking(snapshot.compare(old, new))]


def all_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    return [change.render() for change in snapshot.compare(old, new)]


def test_an_unchanged_document_reports_nothing() -> None:
    assert snapshot.compare(document(), document()) == []


# --- additive: must pass -------------------------------------------------


def test_a_new_endpoint_is_not_breaking() -> None:
    new = document(
        extra_paths={"/api/v1/others": {"get": {"operationId": "list_others", "responses": {}}}}
    )
    assert breaks(document(), new) == []
    assert any("is new" in change for change in all_changes(document(), new))


def test_a_new_optional_request_field_is_not_breaking() -> None:
    new = document(properties={"name": {"type": "string"}, "note": {"type": "string"}})
    assert breaks(document(), new) == []


def test_a_new_response_field_is_not_breaking() -> None:
    new = document()
    new["components"]["schemas"]["Thing"]["properties"]["colour"] = {"type": "string"}
    assert breaks(document(), new) == []


def test_a_new_enum_value_in_a_response_is_not_breaking() -> None:
    old = document(response={"type": "object", "properties": {"state": {"enum": ["a", "b"]}}})
    new = document(response={"type": "object", "properties": {"state": {"enum": ["a", "b", "c"]}}})
    assert breaks(old, new) == []


def test_a_new_accepted_enum_value_in_a_request_is_not_breaking() -> None:
    old = document(properties={"mode": {"enum": ["fast"]}}, required=["mode"])
    new = document(properties={"mode": {"enum": ["fast", "slow"]}}, required=["mode"])
    assert breaks(old, new) == []


def test_widening_a_type_to_allow_null_is_not_breaking() -> None:
    old = document(response={"type": "object", "properties": {"note": {"type": "string"}}})
    new = document(
        response={
            "type": "object",
            "properties": {"note": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        }
    )
    assert breaks(old, new) == []


def test_a_new_documented_status_code_is_not_breaking() -> None:
    old = document()
    new = document(
        responses={
            "200": {
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}
            },
            "429": {"content": {"application/json": {"schema": {"type": "object"}}}},
        }
    )
    assert breaks(old, new) == []


def test_marking_an_operation_deprecated_is_not_breaking() -> None:
    new = document()
    new["paths"]["/api/v1/things"]["post"]["deprecated"] = True
    changes = all_changes(document(), new)
    assert breaks(document(), new) == []
    assert any("deprecated" in change for change in changes)


# --- breaking: must fail -------------------------------------------------


def test_removing_a_path_is_breaking() -> None:
    old = document(extra_paths={"/api/v1/others": {"get": {"operationId": "o", "responses": {}}}})
    assert any(line == "GET /api/v1/others: was removed" for line in breaks(old, document()))


def test_removing_one_method_from_a_kept_path_is_breaking() -> None:
    old = document()
    old["paths"]["/api/v1/things"]["get"] = {"operationId": "get_thing", "responses": {}}
    assert any(line == "GET /api/v1/things: was removed" for line in breaks(old, document()))


def test_removing_a_request_field_is_breaking() -> None:
    old = document(properties={"name": {"type": "string"}, "note": {"type": "string"}})
    assert any("request field 'note' was removed" in line for line in breaks(old, document()))


def test_a_new_required_request_field_is_breaking() -> None:
    new = document(
        properties={"name": {"type": "string"}, "reason": {"type": "string"}},
        required=["name", "reason"],
    )
    assert any("new required request field 'reason'" in line for line in breaks(document(), new))


def test_making_an_existing_optional_request_field_required_is_breaking() -> None:
    old = document(properties={"name": {"type": "string"}, "note": {"type": "string"}})
    new = document(
        properties={"name": {"type": "string"}, "note": {"type": "string"}},
        required=["name", "note"],
    )
    assert any("'note' became required" in line for line in breaks(old, new))


def test_removing_a_response_field_is_breaking() -> None:
    new = document()
    del new["components"]["schemas"]["Thing"]["properties"]["label"]
    new["components"]["schemas"]["Thing"]["required"] = ["id"]
    assert any("response field 'label' was removed" in line for line in breaks(document(), new))


def test_a_response_field_that_stops_being_guaranteed_is_breaking() -> None:
    new = document()
    new["components"]["schemas"]["Thing"]["required"] = ["id"]
    assert any("'label' is no longer always present" in line for line in breaks(document(), new))


def test_changing_a_type_is_breaking() -> None:
    new = document()
    new["components"]["schemas"]["Thing"]["properties"]["id"] = {"type": "integer"}
    assert any(
        "response field 'id' changed type from string to integer" in line
        for line in breaks(document(), new)
    )


def test_narrowing_an_enum_is_breaking() -> None:
    old = document(properties={"mode": {"enum": ["fast", "slow"]}}, required=["mode"])
    new = document(properties={"mode": {"enum": ["fast"]}}, required=["mode"])
    assert any("no longer accepts" in line for line in breaks(old, new))


def test_closing_an_open_field_into_an_enum_is_breaking() -> None:
    old = document(properties={"mode": {"type": "string"}}, required=["mode"])
    new = document(properties={"mode": {"type": "string", "enum": ["fast"]}}, required=["mode"])
    assert any("became a closed set" in line for line in breaks(old, new))


def test_removing_a_documented_status_code_is_breaking() -> None:
    old = document(
        responses={
            "200": {"content": {"application/json": {"schema": {"type": "object"}}}},
            "404": {"content": {"application/json": {"schema": {"type": "object"}}}},
        }
    )
    new = document(
        responses={"200": {"content": {"application/json": {"schema": {"type": "object"}}}}}
    )
    assert any("no longer documents a 404 response" in line for line in breaks(old, new))


def test_a_new_required_query_parameter_is_breaking() -> None:
    old = document(parameters=[])
    new = document(
        parameters=[
            {"name": "since", "in": "query", "required": True, "schema": {"type": "string"}}
        ]
    )
    assert any("new required parameter 'query:since'" in line for line in breaks(old, new))


def test_a_new_optional_query_parameter_is_not_breaking() -> None:
    old = document(parameters=[])
    new = document(
        parameters=[
            {"name": "since", "in": "query", "required": False, "schema": {"type": "string"}}
        ]
    )
    assert breaks(old, new) == []


def test_removing_a_scope_from_an_operation_is_breaking() -> None:
    old = document(security=[{"SessionCookie": []}, {"ApiKeyBearer": ["things:read"]}])
    new = document(security=[{"SessionCookie": []}])
    assert any("no longer accepts ApiKeyBearer(things:read)" in line for line in breaks(old, new))


def test_requiring_a_different_scope_is_breaking() -> None:
    old = document(security=[{"ApiKeyBearer": ["things:read"]}])
    new = document(security=[{"ApiKeyBearer": ["things:admin"]}])
    assert any("no longer accepts ApiKeyBearer(things:read)" in line for line in breaks(old, new))


def test_making_a_public_endpoint_require_authentication_is_breaking() -> None:
    old = document(security=[])
    new = document(security=[{"SessionCookie": []}])
    assert any("previously required none" in line for line in breaks(old, new))


def test_renaming_an_operation_id_is_breaking_because_sdks_use_it() -> None:
    new = document(operation_id="enumerate_things")
    assert any("operationId changed" in line for line in breaks(document(), new))


def test_a_self_referential_schema_terminates() -> None:
    old = document(response={"$ref": "#/components/schemas/Node"})
    old["components"]["schemas"]["Node"] = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
        },
        "required": ["id"],
    }
    assert snapshot.compare(old, old) == []


# --------------------------------------------------------------------------
# The real thing
# --------------------------------------------------------------------------


def test_the_snapshot_is_a_readable_openapi_document() -> None:
    document = snapshot.load_snapshot()
    assert document["openapi"].startswith("3.")
    assert document["info"]["x-api-version"] == "v1"
    assert len(document["paths"]) > 100


def test_the_live_api_makes_no_breaking_change_against_the_snapshot() -> None:
    changes, _ = snapshot.check()
    hard = snapshot.breaking(changes)
    assert not hard, "\n".join(
        ["", *(f"  - {c.render()}" for c in hard), "", snapshot.BREAKING_HINT]
    )


def test_the_snapshot_is_current() -> None:
    changes, identical = snapshot.check()
    lines = [f"  - {change.render()}" for change in changes] or [
        "  - (documentation or metadata only)"
    ]
    assert identical, "\n".join(["", *lines, "", snapshot.UPDATE_HINT])


def test_render_is_stable_and_sorted() -> None:
    text = snapshot.render({"b": 1, "a": {"d": 2, "c": 3}})
    assert text == json.dumps({"a": {"c": 3, "d": 2}, "b": 1}, indent=2) + "\n"


@pytest.mark.parametrize("argv", [["--print"]])
def test_the_cli_can_emit_the_document(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert snapshot.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["info"]["title"] == "Jhin"
