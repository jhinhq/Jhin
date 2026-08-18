"""Alembic graph invariants for the company identity release."""

from alembic.script import ScriptDirectory

from jhin_db.migrate import alembic_config


def test_company_identity_is_the_only_head_and_directly_follows_0013() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("sqlite://"))
    company_identity = scripts.get_revision("0014")

    assert company_identity is not None
    assert company_identity.down_revision == "0013"
    assert scripts.get_heads() == ["0014"]
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
