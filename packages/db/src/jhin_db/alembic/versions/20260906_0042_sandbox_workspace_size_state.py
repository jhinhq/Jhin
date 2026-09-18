"""Say whether a workspace's recorded size is the disk's size or a floor.

``sandbox_workspace.size_bytes`` has always been read as though the walk that
produced it had finished. Most of the time it had. When it had not, the number
was a lower bound wearing the answer's clothes, and every rule downstream --
the per-agent cap, the tenant budget, the eviction order -- acted on it as
fact. A walk that ran 0.44% of the way through a 6.5 GB disk stored 28 MB,
which is under every cap in the product, so the workspace was neither recycled
nor refused; and once one *complete* measurement existed, the ratchet that
kept the larger of the two numbers meant a disk whose later walks all timed
out kept its old total forever, so an agent that had since emptied its disk
was still the fattest row in the tenant and the first one a budget sweep took.

``size_state`` is the missing half of the reading. ``measured`` means the walk
finished and ``size_bytes`` is the disk's usage to the byte. ``unknown`` means
it did not, and that nothing is known about this disk except that it holds at
least ``size_bytes``. The policy that reads it never destroys an ``unknown``
workspace and never treats one as small: it refuses, which costs a run instead
of an agent's unpushed work.

Existing rows are backfilled ``measured``, which is exactly how they are being
read today. It is the only value that does not brick a live deployment: an
``unknown`` row refuses its agent's calls until the disk is emptied, and a
size is only ever refreshed *by* a job, so backfilling ``unknown`` everywhere
would refuse the very jobs that would have produced the truth. The first job
of each workspace after this migration replaces the backfill with a real
answer, which is the same one-job staleness the measurement has always had.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sandbox_workspace",
        sa.Column(
            "size_state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'measured'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sandbox_workspace", "size_state")
