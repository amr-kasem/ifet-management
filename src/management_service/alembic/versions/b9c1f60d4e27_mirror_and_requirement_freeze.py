"""The local Airtable mirror, and the requirement frozen onto each attempt.

Revision ID: b9c1f60d4e27
Revises: a3d8e5c71f04
Create Date: 2026-09-08

**The mirror.** `report-api` must never call Airtable from a request, so the
hierarchy is copied locally and every read serves from the copy — an empty
mirror is an empty picker, not a blocked operator. Four tables, one per level.

Every column corresponds to a field on `mirror.py`'s allowlist and nothing else.
That is the boundary the change document describes to the Airtable team as an
application decision: the token can read all eight tables, including customer
emails, `Approved Proposal Amount` and `Balance Due`, so what protects them is
that there is nowhere here to put them.

**`Value` has no column, deliberately.** It is populated by a PDF extractor that
drops blank cells, so a requirement of `+60/60` can arrive as `9` and every
shifted value is individually plausible (§10.19). A shifted value has already
reached a record marked Passed. The absence of the column is the safety
property, not an oversight to tidy up later.

**`test_results.requirement_snapshot`.** The requirement an attempt was run
against, frozen at start. Without it a published result would carry whatever the
section says at publish time — and requirements do change upstream, so for a
test that has already run that is a false statement rather than a stale one.
"""

import sqlalchemy as sa
from alembic import op

revision = "b9c1f60d4e27"
down_revision = "a3d8e5c71f04"
branch_labels = None
depends_on = None

MIRROR_TABLES = ("at_mirror_projects", "at_mirror_specimens",
                 "at_mirror_protocols", "at_mirror_sections")


def upgrade() -> None:
    op.create_table(
        "at_mirror_projects",
        sa.Column("record_id", sa.String(), primary_key=True),
        sa.Column("job_number", sa.String(), nullable=True),
        sa.Column("project_name", sa.String(), nullable=True),
        sa.Column("mirrored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_at_mirror_projects_job_number",
                    "at_mirror_projects", ["job_number"])

    op.create_table(
        "at_mirror_specimens",
        sa.Column("record_id", sa.String(), primary_key=True),
        sa.Column("specimen_name", sa.String(), nullable=True),
        sa.Column("project_record_id", sa.String(), nullable=True),
        sa.Column("mirrored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_at_mirror_specimens_project_record_id",
                    "at_mirror_specimens", ["project_record_id"])

    op.create_table(
        "at_mirror_protocols",
        sa.Column("record_id", sa.String(), primary_key=True),
        sa.Column("protocol_name", sa.String(), nullable=True),
        sa.Column("specimen_record_id", sa.String(), nullable=True),
        sa.Column("mirrored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_at_mirror_protocols_specimen_record_id",
                    "at_mirror_protocols", ["specimen_record_id"])

    op.create_table(
        "at_mirror_sections",
        sa.Column("record_id", sa.String(), primary_key=True),
        sa.Column("section_name", sa.String(), nullable=True),
        sa.Column("protocol_record_id", sa.String(), nullable=True),
        # The typed requirement fields — the ones added to the Testing base in
        # M1/TA2 so a requirement reaches LabOS machine-readably.
        sa.Column("requirement_code", sa.String(), nullable=True),
        sa.Column("requirement_kind", sa.String(), nullable=True),
        sa.Column("applicability", sa.String(), nullable=True),
        sa.Column("required_value", sa.Float(), nullable=True),
        sa.Column("required_value_inward", sa.Float(), nullable=True),
        sa.Column("required_value_outward", sa.Float(), nullable=True),
        sa.Column("required_unit", sa.String(), nullable=True),
        sa.Column("required_option", sa.String(), nullable=True),
        sa.Column("missile", sa.String(), nullable=True),
        sa.Column("missile_weight", sa.Float(), nullable=True),
        sa.Column("impact_velocity", sa.Float(), nullable=True),
        sa.Column("mirrored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_at_mirror_sections_protocol_record_id",
                    "at_mirror_sections", ["protocol_record_id"])
    op.create_index("ix_at_mirror_sections_requirement_code",
                    "at_mirror_sections", ["requirement_code"])

    op.add_column("test_results",
                  sa.Column("requirement_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("test_results", "requirement_snapshot")
    op.drop_index("ix_at_mirror_sections_requirement_code",
                  table_name="at_mirror_sections")
    op.drop_index("ix_at_mirror_sections_protocol_record_id",
                  table_name="at_mirror_sections")
    op.drop_index("ix_at_mirror_protocols_specimen_record_id",
                  table_name="at_mirror_protocols")
    op.drop_index("ix_at_mirror_specimens_project_record_id",
                  table_name="at_mirror_specimens")
    op.drop_index("ix_at_mirror_projects_job_number",
                  table_name="at_mirror_projects")
    for table in reversed(MIRROR_TABLES):
        op.drop_table(table)
