"""Server-side cycle prevention for reporting chains and team nesting.

Plan sections 6.4/6.5: ``manager_agent_id`` and ``parent_team_id`` form
hierarchies that must stay acyclic. The check is a pure function over an
id -> parent-id mapping so it is trivially unit-testable; services load the
mapping for one workspace and call it inside the write transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID


def would_create_cycle(
    node_id: UUID,
    new_parent_id: UUID | None,
    parents: Mapping[UUID, UUID | None],
) -> bool:
    """True if pointing ``node_id`` at ``new_parent_id`` closes a loop.

    ``parents`` maps every node in the workspace to its current parent
    (manager or parent team). The proposed edge replaces ``node_id``'s entry.
    """
    if new_parent_id is None:
        return False
    if new_parent_id == node_id:
        return True
    seen: set[UUID] = {node_id}
    current: UUID | None = new_parent_id
    while current is not None:
        if current in seen:
            return True
        seen.add(current)
        current = parents.get(current)
    return False
