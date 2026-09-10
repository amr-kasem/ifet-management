"""Impact classification and target velocity — additive only.

Revision ID: a4f18c2d3b90
Revises: c7e4a2b81f56
Create Date: 2026-09-10

TA7a. Three nullable columns and one CHECK constraint on
`missile_impact_tests`. **Nothing is dropped and nothing is narrowed**, which
is the property that matters here: the table already holds 39 tests and 114
shots on the live node, and the deprecated mirror columns
(`at_mirror_sections.missile`/`.missile_weight`/`.impact_velocity`) are
deliberately left in place, to be removed later alongside the Airtable fields
in one coordinated cleanup rather than piecemeal.

`impact_family` is frozen at import from IMPACT_SMI / IMPACT_LMI.
`impact_level` is the operator's D or E, and applies to LMI only.
`target_velocity` is operator-entered ft/s — **not** derived from the
classification, because no authoritative derivation table exists in this
repository or in the contract. It is distinct from `shots.velocity`, which is
the achieved value per impact.

**No backfill.** The 39 existing tests get NULL in all three, and nothing is
inferred from their free-text `missile` or from historical shot velocities.
They remain valid rows: the CHECK is written so a NULL family passes.

**One structural invariant only.** The family and level vocabularies stay in
Pydantic (`IMPACT_FAMILIES`, `IMPACT_LEVELS`) so the business set can widen
without a migration. What the database refuses is the pairing that can never
be meaningful whatever that vocabulary becomes: SMI with a level.

The constraint is written NULL-explicitly rather than with IS DISTINCT FROM.
IS DISTINCT FROM is not portable to the SQLite the local suite runs on, and
the leading IS NULL clause is not redundant — without it the pre-existing rows
pass only because SQL treats a NULL CHECK expression as satisfied, which is
true but is not a statement of intent.
"""
from alembic import op
import sqlalchemy as sa

revision = "a4f18c2d3b90"
down_revision = "c7e4a2b81f56"
branch_labels = None
depends_on = None

_CHECK = "ck_missile_impact_tests_smi_has_no_level"
_CONDITION = ("impact_family IS NULL OR impact_family <> 'SMI' "
              "OR impact_level IS NULL")


def upgrade():
    op.add_column("missile_impact_tests",
                  sa.Column("impact_family", sa.String(), nullable=True))
    op.add_column("missile_impact_tests",
                  sa.Column("impact_level", sa.String(), nullable=True))
    op.add_column("missile_impact_tests",
                  sa.Column("target_velocity", sa.Float(), nullable=True))
    # Named, so the downgrade can drop it by name rather than by guess.
    op.create_check_constraint(_CHECK, "missile_impact_tests", _CONDITION)


def downgrade():
    op.drop_constraint(_CHECK, "missile_impact_tests", type_="check")
    op.drop_column("missile_impact_tests", "target_velocity")
    op.drop_column("missile_impact_tests", "impact_level")
    op.drop_column("missile_impact_tests", "impact_family")
