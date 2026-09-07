"""Manual test capture — Impact, Forced Entry, ANSI Z97.1.

Revision ID: a3f7c21b9e04
Revises: c4e1f8a92b07
Create Date: 2026-09-08

Delivery plan §4.5. The three manually-entered test types get somewhere to live,
and the two requirement values Airtable is asked to supply get somewhere to land.

**This is additive against live production data, not greenfield.** Verified
read-only on the node 2026-09-08: 79 projects, 39 missile impact tests, 114
shots. Nothing here drops a column, narrows a type, or tightens a constraint.
The only changes to existing tables are `add_column` and widening four
`NOT NULL`s to nullable, both of which are safe with rows present.

Ordering note. The live head is `3a65a83e0463`; P1 (`b7c2e9a41d38`) and M2
(`c4e1f8a92b07`) are both unapplied, so production upgrades through all three in
one ordered run. P1 already adds the `airtable_*` columns to `projects`,
`static_tests`, `cyclic_tests` and `test_results` — this revision does not
repeat them. It does add the one P1 missed: the job level, `project_parents`.
"""

import sqlalchemy as sa
from alembic import op

revision = "a3f7c21b9e04"
down_revision = "c4e1f8a92b07"
branch_labels = None
depends_on = None


# The ManualAttempt mixin, as DDL. Applied to both manual_tests (new) and
# missile_impact_tests (existing, populated), so it is written once.
def _attempt_columns():
    return [
        sa.Column("labos_attempt_id", sa.String(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("abort_reason", sa.String(), nullable=True),
        sa.Column("test_result", sa.String(), nullable=True),
        sa.Column("operator_name", sa.String(), nullable=True),
        sa.Column("verdict_by", sa.String(), nullable=True),
        sa.Column("verdict_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retest_required", sa.Boolean(), nullable=True),
        sa.Column("testing_start_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("testing_end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("testing_continued", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    ]


_ATTEMPT_NAMES = [c.name for c in _attempt_columns()]

_PROTOCOL_REF = [
    ("airtable_protocol_id", sa.String()),
    ("airtable_section_id", sa.String()),
    ("airtable_section_name", sa.String()),
]


def upgrade() -> None:
    # ---- manual_tests: Forced Entry + ANSI Z97.1, one table -------------
    #
    # One table with a `type` discriminator rather than two near-identical
    # ones. Both are a pass/fail outcome against a named class or grade; the
    # shape is the same, and `static_tests` already carries a `type` column, so
    # this follows the repo rather than inventing a pattern.
    op.create_table(
        "manual_tests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("required_option", sa.String(), nullable=True),
        sa.Column("result", sa.Boolean(), nullable=True),
        *[sa.Column(n, t, nullable=True) for n, t in _PROTOCOL_REF],
        *_attempt_columns(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_manual_tests_id", "manual_tests", ["id"])
    op.create_index("ix_manual_tests_type", "manual_tests", ["type"])
    op.create_index("ix_manual_tests_labos_attempt_id", "manual_tests",
                    ["labos_attempt_id"], unique=True)
    op.create_index("ix_manual_tests_airtable_section_id", "manual_tests",
                    ["airtable_section_id"])
    op.create_index("ix_manual_tests_airtable_protocol_id", "manual_tests",
                    ["airtable_protocol_id"])

    # ---- missile_impact_tests: attempt identity, and widen ---------------
    #
    # 39 rows are already here. Every added column is nullable, so existing rows
    # remain valid without a backfill; and the four widenings mean an operator
    # records "how many impacts, and whether each passed" without being made to
    # retype metadata the protocol fixes.
    for col in _attempt_columns():
        op.add_column("missile_impact_tests", col)
    for name, type_ in _PROTOCOL_REF:
        op.add_column("missile_impact_tests", sa.Column(name, type_, nullable=True))
    op.create_index("ix_missile_impact_tests_labos_attempt_id",
                    "missile_impact_tests", ["labos_attempt_id"], unique=True)
    op.create_index("ix_missile_impact_tests_airtable_section_id",
                    "missile_impact_tests", ["airtable_section_id"])
    op.create_index("ix_missile_impact_tests_airtable_protocol_id",
                    "missile_impact_tests", ["airtable_protocol_id"])

    op.alter_column("missile_impact_tests", "missile",
                    existing_type=sa.String(), nullable=True)
    op.alter_column("missile_impact_tests", "missile_weight",
                    existing_type=sa.Float(), nullable=True)
    op.alter_column("shots", "area", existing_type=sa.Float(), nullable=True)
    op.alter_column("shots", "velocity", existing_type=sa.Float(), nullable=True)
    op.alter_column("shots", "note", existing_type=sa.String(), nullable=True)

    # ---- test_photos ------------------------------------------------------
    #
    # Two nullable FKs rather than a polymorphic owner: the owner set is two and
    # closed, and real foreign keys let the database enforce it. Originals stay
    # on the node under `uploads/`; Airtable receives a downscaled preview via
    # the attachment channel.
    op.create_table(
        "test_photos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manual_test_id", sa.Integer(), nullable=True),
        sa.Column("missile_impact_test_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["manual_test_id"], ["manual_tests.id"]),
        sa.ForeignKeyConstraint(["missile_impact_test_id"],
                                ["missile_impact_tests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_test_photos_id", "test_photos", ["id"])

    # ---- requirement values that had nowhere to land ---------------------
    #
    # Both nullable, and that is the point: in the production Airtable base
    # `# Dials` and `Impact` are blank on most Protocol Sections, and a missing
    # requirement must never read as zero (write contract §5).
    op.add_column("projects", sa.Column("gauge_count", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("impact_count", sa.Integer(), nullable=True))

    # Class 3 display metadata — Product Type, Height, Width, Service line.
    # Shown to the operator so it is not retyped; never executed from.
    op.add_column("projects", sa.Column("airtable_meta", sa.JSON(), nullable=True))

    # P1 linked `projects`; the job level was missed.
    op.add_column("project_parents",
                  sa.Column("airtable_project_id", sa.String(), nullable=True))
    op.create_index("ix_project_parents_airtable_project_id", "project_parents",
                    ["airtable_project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_parents_airtable_project_id",
                  table_name="project_parents")
    op.drop_column("project_parents", "airtable_project_id")
    op.drop_column("projects", "airtable_meta")
    op.drop_column("projects", "impact_count")
    op.drop_column("projects", "gauge_count")

    op.drop_index("ix_test_photos_id", table_name="test_photos")
    op.drop_table("test_photos")

    # Restoring NOT NULL would fail against rows this revision made legal, so
    # the widenings are reverted only as far as is actually safe: the columns
    # keep their types and stay nullable. A downgrade is a rollback of
    # structure, not an invitation to delete an operator's data.
    op.drop_index("ix_missile_impact_tests_airtable_protocol_id",
                  table_name="missile_impact_tests")
    op.drop_index("ix_missile_impact_tests_airtable_section_id",
                  table_name="missile_impact_tests")
    op.drop_index("ix_missile_impact_tests_labos_attempt_id",
                  table_name="missile_impact_tests")
    for name, _ in _PROTOCOL_REF:
        op.drop_column("missile_impact_tests", name)
    for name in reversed(_ATTEMPT_NAMES):
        op.drop_column("missile_impact_tests", name)

    op.drop_index("ix_manual_tests_airtable_protocol_id", table_name="manual_tests")
    op.drop_index("ix_manual_tests_airtable_section_id", table_name="manual_tests")
    op.drop_index("ix_manual_tests_labos_attempt_id", table_name="manual_tests")
    op.drop_index("ix_manual_tests_type", table_name="manual_tests")
    op.drop_index("ix_manual_tests_id", table_name="manual_tests")
    op.drop_table("manual_tests")
