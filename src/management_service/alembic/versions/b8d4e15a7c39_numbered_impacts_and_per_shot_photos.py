"""Numbered impacts, each with its own value and its own photographs.

Revision ID: b8d4e15a7c39
Revises: a3f7c21b9e04
Create Date: 2026-09-08

An impact test is a *sequence*: impact 1, impact 2, impact 3, each with its own
outcome and its own photographs. `a3f7c21b9e04` modelled shots as an unordered
set attached to the test, with photographs at the test level only. That loses two
things an operator needs — which impact is which, and which photograph shows
which impact.

Two additive changes:

1. `shots.shot_number` — the ordinal the operator sees. Backfilled for the 114
   production rows by their existing insertion order within each test, which is
   the order they were recorded in, then made NOT NULL. New on this revision, so
   tightening it is not narrowing an existing column.

2. `test_photos.shot_id` — a photograph may now belong to one impact rather than
   to the attempt as a whole. Nullable: attempt-level photographs stay valid,
   and Forced Entry and ANSI Z97.1 only ever have those.
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d4e15a7c39"
down_revision = "a3f7c21b9e04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shots", sa.Column("shot_number", sa.Integer(), nullable=True))

    # Backfill by insertion order within each test. `id` is a sequence, so for
    # rows recorded through the old path it is the order they happened in - the
    # best evidence available, and better than leaving 114 rows unnumbered.
    op.execute("""
        UPDATE shots AS s SET shot_number = ordered.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY missile_impact_test_id ORDER BY id) AS rn
            FROM shots
        ) AS ordered
        WHERE s.id = ordered.id
    """)
    # Any orphan with no parent test still needs a value before NOT NULL.
    op.execute("UPDATE shots SET shot_number = 1 WHERE shot_number IS NULL")
    op.alter_column("shots", "shot_number",
                    existing_type=sa.Integer(), nullable=False)

    # One number per impact, per test. Two impacts numbered 3 is not a thing.
    op.create_unique_constraint("uq_shots_test_number", "shots",
                                ["missile_impact_test_id", "shot_number"])

    op.add_column("test_photos", sa.Column("shot_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_test_photos_shot_id", "test_photos", "shots",
                          ["shot_id"], ["id"])
    op.create_index("ix_test_photos_shot_id", "test_photos", ["shot_id"])


def downgrade() -> None:
    op.drop_index("ix_test_photos_shot_id", table_name="test_photos")
    op.drop_constraint("fk_test_photos_shot_id", "test_photos", type_="foreignkey")
    op.drop_column("test_photos", "shot_id")

    op.drop_constraint("uq_shots_test_number", "shots", type_="unique")
    op.drop_column("shots", "shot_number")
