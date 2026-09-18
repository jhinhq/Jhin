import pytest
from pydantic import BaseModel

from jhin_db.models import Task
from jhin_domain import new_uuid7
from jhin_policy import RiskLevel, ToolDefinition
from jhin_tools.builtin import ToolCatalog
from jhin_tools.gateway import ToolGateway


class Input(BaseModel):
    pass


@pytest.mark.asyncio
async def test_plan_turn_blocks_write_even_with_wildcard_grant(context):
    from jhin_db.models import AgentCapabilityGrant

    context.session.add(
        Task(
            id=context.task_id,
            workspace_id=context.workspace_id,
            title="Plan",
            correlation_id=new_uuid7(),
            metadata_json={"execution_mode": "plan"},
        )
    )
    context.session.add(
        AgentCapabilityGrant(
            workspace_id=context.workspace_id,
            agent_id=context.agent_id,
            capability="*",
            effect="allow",
        )
    )
    await context.session.flush()
    called = []

    async def executor(ctx, value):
        called.append(True)
        return Input()

    catalog = ToolCatalog()
    catalog.register(
        ToolDefinition(
            name="example.write",
            description="Write",
            risk=RiskLevel.WRITE,
            input_model=Input,
            output_model=Input,
            required_capability="example.write",
        ),
        executor,
    )
    outcome = await ToolGateway(context, catalog).request("example.write", "{}")
    assert outcome.status == "denied" and outcome.error_code == "turn_mode_read_only"
    assert called == []
