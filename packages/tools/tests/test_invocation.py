"""Stable, bounded runtime tool invocation identities."""

from uuid import UUID

import pytest

from jhin_tools import (
    SYNC_INVOCATION_FORMAT_VERSION,
    SYNC_INVOCATION_NAMESPACE,
    TOOL_INVOCATION_FORMAT_VERSION,
    TOOL_INVOCATION_NAMESPACE,
    stable_sync_invocation_id,
    stable_tool_invocation_id,
)


def test_runtime_tool_invocation_id_is_versioned_and_stable() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")

    invocation_id = stable_tool_invocation_id(run_id, 4, 2)

    assert TOOL_INVOCATION_FORMAT_VERSION == 1
    assert str(TOOL_INVOCATION_NAMESPACE) == "4f0ac960-eab4-5f17-9b65-9f9bcbf3e0a8"
    assert str(invocation_id) == "8003464f-8e3a-5d15-8401-f36ba357894a"
    assert invocation_id == stable_tool_invocation_id(run_id, 4, 2)
    assert invocation_id != stable_tool_invocation_id(
        UUID("00000000-0000-0000-0000-000000000002"), 4, 2
    )
    assert invocation_id != stable_tool_invocation_id(run_id, 5, 2)
    assert invocation_id != stable_tool_invocation_id(run_id, 4, 3)


@pytest.mark.parametrize(
    ("step_index", "call_ordinal"),
    [(-1, 0), (1_000_001, 0), (0, -1), (0, 256)],
)
def test_runtime_tool_invocation_id_rejects_out_of_range_components(
    step_index: int, call_ordinal: int
) -> None:
    with pytest.raises(ValueError):
        stable_tool_invocation_id(
            UUID("00000000-0000-0000-0000-000000000001"),
            step_index,
            call_ordinal,
        )


def test_trigger_sync_invocation_id_is_versioned_and_stable() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")

    invocation_id = stable_sync_invocation_id(run_id)

    assert SYNC_INVOCATION_FORMAT_VERSION == 1
    assert str(SYNC_INVOCATION_NAMESPACE) == "3dc26b04-1af9-5ec5-a0ea-d7d95c3a393b"
    assert str(invocation_id) == "210c5cc6-4dc3-5586-ad3d-8212b37ba182"
    assert invocation_id == stable_sync_invocation_id(run_id)
    assert invocation_id != stable_sync_invocation_id(UUID("00000000-0000-0000-0000-000000000002"))
