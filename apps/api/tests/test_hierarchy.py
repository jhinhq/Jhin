"""Unit tests for manager-chain / team-nesting cycle prevention."""

from uuid import UUID

from jhin_api.org.hierarchy import would_create_cycle
from jhin_domain import new_uuid7


def chain(*pairs: tuple[UUID, UUID | None]) -> dict[UUID, UUID | None]:
    return dict(pairs)


def test_self_reference_is_a_cycle() -> None:
    node = new_uuid7()
    assert would_create_cycle(node, node, {node: None})


def test_clearing_parent_is_never_a_cycle() -> None:
    node = new_uuid7()
    assert not would_create_cycle(node, None, {node: None})


def test_direct_two_node_cycle() -> None:
    cto, swe = new_uuid7(), new_uuid7()
    # SWE already reports to CTO; making CTO report to SWE closes the loop.
    parents = chain((cto, None), (swe, cto))
    assert would_create_cycle(cto, swe, parents)


def test_deep_cycle_through_chain() -> None:
    a, b, c, d = (new_uuid7() for _ in range(4))
    parents = chain((a, None), (b, a), (c, b), (d, c))
    assert would_create_cycle(a, d, parents)


def test_valid_reassignment_within_tree() -> None:
    root, mid, leaf, other = (new_uuid7() for _ in range(4))
    parents = chain((root, None), (mid, root), (leaf, mid), (other, root))
    # Moving leaf under other is fine.
    assert not would_create_cycle(leaf, other, parents)


def test_new_node_can_point_at_existing_chain() -> None:
    root, mid = new_uuid7(), new_uuid7()
    new_node = new_uuid7()
    parents = chain((root, None), (mid, root))
    assert not would_create_cycle(new_node, mid, parents)


def test_parent_missing_from_map_terminates() -> None:
    node, ghost = new_uuid7(), new_uuid7()
    # Ghost has no entry in the map (dangling reference): walk must stop.
    assert not would_create_cycle(node, ghost, {node: None})
