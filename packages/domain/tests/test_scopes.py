"""The API-key scope taxonomy and the ceiling rule."""

from jhin_domain import (
    ALL_SCOPE_KEYS,
    CATEGORIES,
    CATEGORY_SCOPES,
    SCOPE_BY_KEY,
    SCOPES,
    WorkspaceRole,
    effective_scopes,
    expand_scopes,
    is_known_scope,
    scopes_above_role,
    scopes_for_role,
)


def test_every_scope_is_category_colon_action_and_uniquely_keyed() -> None:
    assert len(SCOPE_BY_KEY) == len(SCOPES)
    for scope in SCOPES:
        assert scope.key == f"{scope.category}:{scope.action}"
        assert scope.action != "*", "wildcards are computed, never declared"
        assert scope.label and scope.description


def test_every_category_has_scopes_and_every_scope_has_a_category() -> None:
    declared = {category.key for category in CATEGORIES}
    used = {scope.category for scope in SCOPES}
    assert declared == used
    for category in CATEGORIES:
        assert CATEGORY_SCOPES[category.key], f"{category.key} declares no scopes"


def test_wildcard_expands_to_exactly_its_category() -> None:
    assert expand_scopes(["chats:*"]) == {scope.key for scope in CATEGORY_SCOPES["chats"]}
    assert "agents:read" not in expand_scopes(["chats:*"])


def test_unknown_and_global_wildcards_are_not_grantable() -> None:
    assert not is_known_scope("*")
    assert not is_known_scope("*:*")
    assert not is_known_scope("chats:delete")
    assert expand_scopes(["*", "*:*", "chats:delete"]) == frozenset()


def test_expand_scopes_ignores_non_string_and_non_list_input() -> None:
    assert expand_scopes(None) == frozenset()
    assert expand_scopes("chats:read") == frozenset()
    assert expand_scopes([1, None, "chats:read"]) == {"chats:read"}


def test_role_ceilings_are_monotonic() -> None:
    viewer = scopes_for_role(WorkspaceRole.VIEWER)
    member = scopes_for_role(WorkspaceRole.MEMBER)
    admin = scopes_for_role(WorkspaceRole.ADMIN)
    owner = scopes_for_role(WorkspaceRole.OWNER)
    assert viewer < member < admin
    assert admin == owner == ALL_SCOPE_KEYS


def test_a_viewer_ceiling_carries_only_reads_and_its_own_key_management() -> None:
    writes = {
        key for key in scopes_for_role(WorkspaceRole.VIEWER) if SCOPE_BY_KEY[key].action != "read"
    }
    # The one non-read a viewer may hold is minting their own read-only key.
    assert writes == {"api_keys:write"}


def test_the_intersection_rule_strips_scopes_above_the_ceiling() -> None:
    requested = ["chats:write", "agents:write", "audit:read"]
    assert effective_scopes(requested, WorkspaceRole.MEMBER) == {"chats:write"}
    assert effective_scopes(requested, WorkspaceRole.ADMIN) == set(requested)


def test_a_member_key_asking_for_everything_still_gets_only_member_scopes() -> None:
    everything = [f"{category.key}:*" for category in CATEGORIES]
    granted = effective_scopes(everything, WorkspaceRole.MEMBER)
    assert granted == scopes_for_role(WorkspaceRole.MEMBER)
    assert "agents:write" not in granted
    assert "members:write" not in granted


def test_scopes_above_role_names_exactly_what_was_refused() -> None:
    above = scopes_above_role(["chats:write", "audit:read", "apps:write"], WorkspaceRole.MEMBER)
    assert above == ("apps:write", "audit:read")
    assert scopes_above_role(["chats:write"], WorkspaceRole.MEMBER) == ()
