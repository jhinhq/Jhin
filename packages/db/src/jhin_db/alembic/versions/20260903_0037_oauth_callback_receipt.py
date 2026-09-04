"""A consumed authorization remembers what it produced
(``docs/architecture/oauth.md``).

Three columns on ``oauth_authorization``, and one reason for all three: the
OAuth ``state`` is single-use by design, and a browser spends it more than
once for reasons nobody chose — a link prefetch, a refresh, a back-button, a
Cloudflare Access re-issue in the middle of the round trip. Today the second
request finds no row and is refused, even when the first request created the
connection perfectly. The person is told their sign-in link is dead while
looking at a connection that exists.

*``outcome``* is what the row produced, from a ten-value vocabulary a check
constraint pins down. It is never provider prose and never a secret: one of
``connected``, the eight landings the browser can be shown, or one of the two
manifest results.

*``outcome_connection_id``* is the connection the flow concerns — the one it
created, or the one it was re-authorizing — so a repeat can send the browser
to the same page without minting anything. ``ON DELETE SET NULL`` on purpose,
in both directions of the trade: a receipt must never be the reason a deleted
connection stays referenced, and must never itself vanish when one is deleted.

*``retain_until``* is the garbage horizon, and it is a second column rather
than a reuse of ``expires_at`` because the two windows are different
questions. ``expires_at`` is how long the row may be *claimed* — the security
bound. ``retain_until`` is how long a spent row is *kept* — a usability
window. Conflating them would force one number to be both.

The backfill is what makes this correct on a populated table: every
pre-existing row is pending, so ``retain_until = expires_at`` reproduces
today's purge behaviour exactly, and ``purge_expired`` can switch to the new
column without a change in what it deletes.

``downgrade`` drops what ``upgrade`` created. Any receipt still outstanding
goes with it, which costs at most a repeated callback landing on the generic
recovery page — the behaviour before this revision.

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The closed vocabulary a settled row may remember. Kept literal here rather
#: than imported: a migration means what it meant on the day it ran.
_OUTCOMES = (
    "connected",
    "denied",
    "failed",
    "client_rejected",
    "callback_mismatch",
    "redirect_changed",
    "issuer_mismatch",
    "registration_gone",
    "github_app_created",
    "github_app_failed",
)


def upgrade() -> None:
    op.add_column("oauth_authorization", sa.Column("outcome", sa.String(32), nullable=True))
    op.add_column(
        "oauth_authorization", sa.Column("outcome_connection_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_oauth_authorization_outcome_connection",
        "oauth_authorization",
        "connection",
        ["outcome_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Added nullable, backfilled, then made NOT NULL: every existing row is
    # pending, so mirroring ``expires_at`` reproduces today's purge exactly.
    op.add_column(
        "oauth_authorization", sa.Column("retain_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE oauth_authorization SET retain_until = expires_at WHERE retain_until IS NULL"
        )
    )
    op.alter_column("oauth_authorization", "retain_until", nullable=False)
    op.create_index("ix_oauth_authorization_retain_until", "oauth_authorization", ["retain_until"])
    values = ",".join(f"'{outcome}'" for outcome in _OUTCOMES)
    op.create_check_constraint(
        "ck_oauth_authorization_outcome",
        "oauth_authorization",
        f"outcome IS NULL OR outcome IN ({values})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_oauth_authorization_outcome", "oauth_authorization", type_="check")
    op.drop_index("ix_oauth_authorization_retain_until", table_name="oauth_authorization")
    op.drop_column("oauth_authorization", "retain_until")
    op.drop_constraint(
        "fk_oauth_authorization_outcome_connection", "oauth_authorization", type_="foreignkey"
    )
    op.drop_column("oauth_authorization", "outcome_connection_id")
    op.drop_column("oauth_authorization", "outcome")
