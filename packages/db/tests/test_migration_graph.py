"""Alembic graph invariants for the company identity, conversations, and memory
releases."""

from alembic.script import ScriptDirectory

from jhin_db.migrate import alembic_config


def test_company_identity_is_the_only_head_and_directly_follows_0013() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    company_identity = scripts.get_revision("0014")

    assert company_identity is not None
    assert company_identity.down_revision == "0013"
    assert [revision.revision for revision in scripts.walk_revisions("base", "0014")] == [
        "0014",
        "0013",
        "0012",
        "0011",
        "0010",
        "0009",
        "0008",
        "0007",
        "0006",
        "0005",
        "0004",
        "0003",
        "0002",
        "0001",
    ]


def test_conversations_directly_follows_0014() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    conversations = scripts.get_revision("0015")

    assert conversations is not None
    assert conversations.down_revision == "0014"
    assert [revision.revision for revision in scripts.walk_revisions("base", "0015")][:2] == [
        "0015",
        "0014",
    ]


def test_memory_directly_follows_0015_and_is_in_the_linear_chain() -> None:
    """0016 sits on 0015. It is deliberately *not* asserted to be the head:
    later releases (media 0017, coordination 0018) chain on top of it."""
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    memory = scripts.get_revision("0016")

    assert memory is not None
    assert memory.down_revision == "0015"
    chain = [revision.revision for revision in scripts.walk_revisions("base", "0016")]
    assert chain[:3] == ["0016", "0015", "0014"]
    assert len(scripts.get_heads()) == 1, "the migration graph must stay linear"


def test_media_directly_follows_memory() -> None:
    """0017 (media) is additive on top of 0016 (memory); the head is not
    asserted because coordination (0018) lands independently."""
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    media = scripts.get_revision("0017")

    assert media is not None
    assert media.down_revision == "0016"


def test_coordination_directly_follows_0017() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    coordination = scripts.get_revision("0018")

    assert coordination is not None
    assert coordination.down_revision == "0017"


def test_review_parking_directly_follows_0018() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    parking = scripts.get_revision("0019")

    assert parking is not None
    assert parking.down_revision == "0018"


def test_provider_billing_directly_follows_0019_and_is_the_head() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    billing = scripts.get_revision("0020")

    assert billing is not None
    assert billing.down_revision == "0019"
    assert scripts.get_heads() == ["0020"]
