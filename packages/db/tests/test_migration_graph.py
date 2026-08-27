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


def test_provider_billing_directly_follows_0019() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    billing = scripts.get_revision("0020")

    assert billing is not None
    assert billing.down_revision == "0019"


def test_shape_avatars_directly_follows_0020() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    shapes = scripts.get_revision("0021")

    assert shapes is not None
    assert shapes.down_revision == "0020"


def test_skills_directly_follows_0021() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    skills = scripts.get_revision("0022")

    assert skills is not None
    assert skills.down_revision == "0021"


def test_skill_authorship_directly_follows_0022_and_the_graph_stays_linear() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    authorship = scripts.get_revision("0023")

    assert authorship is not None
    assert authorship.down_revision == "0022"
    assert len(scripts.get_heads()) == 1, "the migration graph must stay linear"


def test_skill_category_directly_follows_0023() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    category = scripts.get_revision("0024")

    assert category is not None
    assert category.down_revision == "0023"


def test_skill_category_backfill_directly_follows_0024_and_the_graph_stays_linear() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    backfill = scripts.get_revision("0025")

    assert backfill is not None
    assert backfill.down_revision == "0024"
    assert len(scripts.get_heads()) == 1, "the migration graph must stay linear"


def test_access_control_directly_follows_0025() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    access = scripts.get_revision("0026")

    assert access is not None
    assert access.down_revision == "0025"
    assert len(scripts.get_heads()) == 1, "the migration graph must stay linear"


def test_measured_pricing_directly_follows_0026() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    pricing = scripts.get_revision("0027")

    assert pricing is not None
    assert pricing.down_revision == "0026"
    assert len(scripts.get_heads()) == 1, "the migration graph must stay linear"


def test_catalog_attribution_directly_follows_0027() -> None:
    """Split from 0027 deliberately: 0027 had already shipped to a running
    database, and an applied migration must never be edited in place."""
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    attribution = scripts.get_revision("0028")

    assert attribution is not None
    assert attribution.down_revision == "0027"


def test_membership_settings_directly_follows_0028() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    membership_settings = scripts.get_revision("0029")

    assert membership_settings is not None
    assert membership_settings.down_revision == "0028"


def test_mcp_server_slug_unique_directly_follows_0029_and_is_the_head() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    server_slug = scripts.get_revision("0030")

    assert server_slug is not None
    assert server_slug.down_revision == "0029"
    assert list(scripts.get_heads()) == ["0030"], "the migration graph must stay linear"
