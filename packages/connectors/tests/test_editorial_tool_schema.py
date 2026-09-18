"""The real nested editorial brief survives advertisement and provider serialization."""

import json
from dataclasses import asdict
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from jhin_agent_worker.reasoning import to_model_tool_schemas
from jhin_connectors.ghost.assignments import ASSIGNMENT_TOOLS, AssignmentCreateInput
from jhin_connectors.registry import build_default_definition_catalog
from jhin_models import ModelMessage, ModelRequest
from jhin_models.providers.ollama import OllamaClient
from jhin_models.providers.openrouter import OpenRouterClient
from jhin_models.tool_schemas import inline_local_schema_refs
from jhin_workflows.agent_task.shared import AdvertisedTool


@pytest.mark.parametrize("provider", ["openrouter", "ollama"])
async def test_nested_brief_schema_reaches_provider_intact(provider: str) -> None:
    definition = next(
        item for item, _ in ASSIGNMENT_TOOLS if item.name == "ghost.assignment.create"
    )
    original = definition.input_json_schema()
    # The Temporal advertised-tool boundary is serialized before reasoning.
    advertised = AdvertisedTool(definition.name, definition.description, original)
    transported = AdvertisedTool(**json.loads(json.dumps(asdict(advertised))))
    model_tools = to_model_tool_schemas([transported])
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "model": "fixture",
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = (
        OpenRouterClient(api_key="fixture", transport=transport)
        if provider == "openrouter"
        else OllamaClient(transport=transport)
    )
    try:
        await client.generate(
            ModelRequest(
                model="fixture",
                tools=model_tools,
                messages=(ModelMessage(role="user", content="Create an editorial assignment"),),
            )
        )
    finally:
        await client.close()
    parameters = seen["tools"][0]["function"]["parameters"]
    if provider == "openrouter":
        assert parameters == original
        assert parameters["properties"]["brief"] == {"$ref": "#/$defs/EditorialBrief"}
        brief = parameters["$defs"]["EditorialBrief"]
    else:
        # Ollama decodes each property into ToolProperty, which discards $ref.
        # The brief must carry its actual type/properties at this boundary.
        assert "$defs" not in parameters
        brief = parameters["properties"]["brief"]
        assert brief == original["$defs"]["EditorialBrief"]
    assert brief["type"] == "object" and brief["additionalProperties"] is False
    assert brief["properties"]["image_mode"]["enum"] == ["none", "cover", "cover_and_inline"]
    assert brief["properties"]["must_include"]["items"]["type"] == "string"
    assert "brief" in parameters["required"]


@pytest.mark.parametrize("brief", ['{"topic":"fixture"}', "Write about a topic."])
def test_brief_strings_stay_rejected(brief: str) -> None:
    with pytest.raises(ValidationError) as error:
        AssignmentCreateInput.model_validate({"connection_id": "fixture", "brief": brief})
    assert error.value.errors(include_input=False)[0]["type"] == "model_type"


def test_all_shipped_tool_schemas_fit_ollama_reference_expansion() -> None:
    for definition in build_default_definition_catalog().definitions():
        schema = inline_local_schema_refs(definition.input_json_schema())
        assert schema["type"] == "object", definition.name
