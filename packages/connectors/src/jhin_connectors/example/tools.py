"""Tool definitions + executors for the example connector.

The executor demonstrates the mandatory pattern: resolve the connection
(workspace-scoped, credentials decrypted at execution time, plan 13.5), use
the credential in process memory only, and return a schema-validated output
model. Real connectors make httpx calls where this one echoes.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel

from jhin_connectors.example.schemas import PingInput, PingOutput
from jhin_connectors.execution import resolve_connection
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolExecutionContext, ToolExecutor


async def _ping(ctx: ToolExecutionContext, payload: BaseModel) -> BaseModel:
    data = cast(PingInput, payload)
    resolved = await resolve_connection(ctx, data.connection_id, connector_type="example")
    # The credential is available here (resolved.credentials["api_key"]) for
    # authenticating a real API call; it must never appear in the output.
    return PingOutput(reply=f"pong: {data.message}", connection_name=resolved.connection.name)


EXAMPLE_TOOLS: tuple[tuple[ToolDefinition, ToolExecutor], ...] = (
    (
        ToolDefinition(
            name="example.ping",
            description="Echo a message through an example connection.",
            risk=RiskLevel.READ,
            input_model=PingInput,
            output_model=PingOutput,
            required_capability="example.ping",
            scope_keys=("connection_id",),
        ),
        _ping,
    ),
)
