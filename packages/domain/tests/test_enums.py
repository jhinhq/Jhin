from jhin_domain import WorkspaceRole, new_uuid7, role_satisfies


def test_role_ordering_owner_satisfies_everything() -> None:
    for required in WorkspaceRole:
        assert role_satisfies(WorkspaceRole.OWNER, required)


def test_role_ordering_viewer_only_satisfies_viewer() -> None:
    assert role_satisfies(WorkspaceRole.VIEWER, WorkspaceRole.VIEWER)
    assert not role_satisfies(WorkspaceRole.VIEWER, WorkspaceRole.MEMBER)
    assert not role_satisfies(WorkspaceRole.MEMBER, WorkspaceRole.ADMIN)
    assert not role_satisfies(WorkspaceRole.ADMIN, WorkspaceRole.OWNER)


def test_admin_satisfies_member_and_viewer() -> None:
    assert role_satisfies(WorkspaceRole.ADMIN, WorkspaceRole.MEMBER)
    assert role_satisfies(WorkspaceRole.ADMIN, WorkspaceRole.VIEWER)


def test_uuid7_is_time_ordered() -> None:
    first, second = new_uuid7(), new_uuid7()
    assert first.version == 7
    assert first < second
