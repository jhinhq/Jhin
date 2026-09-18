"""Ollama tool schemas carry nested types without unresolved local references."""

from copy import deepcopy

import pytest

from jhin_models import ModelProviderError
from jhin_models.tool_schemas import inline_local_schema_refs


def test_nested_references_arrays_and_nullable_unions_preserve_constraints() -> None:
    schema = {
        "$defs": {
            "Tag": {"type": "string", "enum": ["a", "b"]},
            "Brief": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tags": {"type": "array", "maxItems": 3, "items": {"$ref": "#/$defs/Tag"}}
                },
                "required": ["tags"],
            },
        },
        "type": "object",
        "properties": {"brief": {"anyOf": [{"$ref": "#/$defs/Brief"}, {"type": "null"}]}},
    }
    original = deepcopy(schema)
    output = inline_local_schema_refs(schema)
    brief = output["properties"]["brief"]["anyOf"][0]
    assert brief["type"] == "object" and brief["additionalProperties"] is False
    assert brief["required"] == ["tags"]
    assert brief["properties"]["tags"] == {
        "type": "array",
        "maxItems": 3,
        "items": {"type": "string", "enum": ["a", "b"]},
    }
    assert output["properties"]["brief"]["anyOf"][1] == {"type": "null"}
    assert "$defs" not in output and schema == original


def test_reference_siblings_and_escaped_json_pointer_preserve_property_description() -> None:
    schema = {
        "$defs": {"a/b~c": {"type": "object", "description": "Base", "properties": {}}},
        "type": "object",
        "properties": {
            "brief": {
                "$ref": "#/$defs/a~1b~0c",
                "description": "Use this object",
                "additionalProperties": False,
            }
        },
    }
    assert inline_local_schema_refs(schema)["properties"]["brief"] == {
        "type": "object",
        "properties": {},
        "description": "Use this object",
        "additionalProperties": False,
    }


def test_literal_schema_like_defaults_are_not_interpreted_as_references() -> None:
    literal = {"$ref": "https://example.test/data", "$defs": {"value": "data"}}
    schema = {"type": "object", "properties": {"payload": {"type": "object", "default": literal}}}
    assert inline_local_schema_refs(schema) == schema


@pytest.mark.parametrize("ref", ["#/$defs/Missing", "https://example.test/schema", "#/$defs/Loop"])
def test_invalid_external_or_cyclic_refs_fail_before_provider_request(ref: str) -> None:
    schema = {
        "$defs": {"Loop": {"type": "object", "properties": {"child": {"$ref": ref}}}},
        "type": "object",
        "properties": {"value": {"$ref": ref}},
    }
    with pytest.raises(ModelProviderError) as error:
        inline_local_schema_refs(schema)
    assert not error.value.retryable
    assert ref not in str(error.value)


def test_exponential_reference_expansion_is_bounded() -> None:
    definitions = {"0": {"type": "string"}}
    for index in range(1, 20):
        definitions[str(index)] = {
            "type": "object",
            "properties": {name: {"$ref": f"#/$defs/{index - 1}"} for name in ("left", "right")},
        }
    with pytest.raises(ModelProviderError, match="limit"):
        inline_local_schema_refs({"$defs": definitions, "$ref": "#/$defs/19"})


def test_conflicting_ref_constraints_are_not_silently_overridden() -> None:
    with pytest.raises(ModelProviderError):
        inline_local_schema_refs(
            {
                "$defs": {"Count": {"type": "integer", "maximum": 3}},
                "$ref": "#/$defs/Count",
                "maximum": 10,
            }
        )


def test_deep_schema_and_oversized_property_names_are_bounded() -> None:
    deep: dict = {"type": "string"}
    for _ in range(70):
        deep = {"type": "array", "items": deep}
    for schema in (deep, {"type": "object", "properties": {"x" * 262_145: {"type": "string"}}}):
        with pytest.raises(ModelProviderError, match="limit"):
            inline_local_schema_refs(schema)
