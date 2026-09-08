"""Operator identity captured at run start, on the test the rig will run.

Revision ID: a3d8e5c71f04
Revises: f7b2c04e19a5
Create Date: 2026-09-08

**Why a column on the test and not a firmware change.**

Contract §4.5 requires an operator on every terminal write, and the rig's
`POST .../trials` callback carries only `deflections`. The obvious reading is
that firmware must start sending an operator — and that reading is wrong, which
matters because it would have made a coordinated firmware release a dependency
of the whole outbound path.

The operator is not a fact the *rig* knows. It is declared by a person in the UI
when they begin a run, before any hardware moves, and `identity_assurance =
declared` (contract §4) says exactly that: it is an assertion by whoever is at
the screen, never something a machine measures. So the right place to capture it
is the run-start request LabOS already receives from the UI, and the right place
to keep it is the test row the rig will report against. The hardware callback
then inherits it.

That leaves the firmware wire contract untouched: `deflections` alone still
works, and the attempt it produces is now completable.

A firmware change *would* be necessary for facts only the rig has — which
callback belongs to which run when two could overlap (DG1/DG2's `event_id`),
or the achieved pressure that nothing currently persists (TC4). Operator
identity is not one of those, and the distinction is worth keeping straight.
"""

import sqlalchemy as sa
from alembic import op

revision = "a3d8e5c71f04"
down_revision = "f7b2c04e19a5"
branch_labels = None
depends_on = None

# All four, because the column lives on the `AirtableProtocolRef` mixin so every
# test type captures it identically — the manual types already take an operator
# at `POST .../trials`, but the ORM requires the column to exist on their tables
# too, and a divergence here is how the two creation paths drifted before.
TABLES = ("static_tests", "cyclic_tests", "manual_tests",
          "missile_impact_tests")


def upgrade() -> None:
    for table in TABLES:
        # Nullable: existing rows predate run-start capture, and a test that has
        # not been started has no operator. Absent stays distinguishable from
        # "nobody" — the same rule the review columns follow.
        op.add_column(table, sa.Column("operator_name", sa.String(),
                                       nullable=True))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "operator_name")
