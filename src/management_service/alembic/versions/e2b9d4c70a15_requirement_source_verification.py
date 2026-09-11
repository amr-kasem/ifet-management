"""Requirement source verification — additive only.

Revision ID: e2b9d4c70a15
Revises: a4f18c2d3b90
Create Date: 2026-09-11

DG14 / contract §3.3. Six nullable columns on `projects` recording the
design-pressure pair as read off the trusted proposal by a named person, with
the document reference and the time.

**Why a column and not a rule.** LabOS reads the typed design-pressure pair
from Airtable and derives all fourteen static and cyclic stages from it, and
the upstream extractor is known to shift values one column to the left. A
shifted 60 arrives as 9: a number, in the right column, with the right unit, of
the right kind. Nothing can inspect it and tell. What can be done is require a
second, independent reading of the same fact and refuse to run while the two
disagree — and a second reading has to be stored somewhere.

**No backfill, and there cannot be one.** These columns say a named person read
a named document at a named time. There is no evidence for that claim about any
row that already exists, and inventing one would be worse than leaving it
absent. Existing projects get NULL in all six and become non-executable *only
if they are Airtable-bound* — a LabOS-only job never needed them and is
unaffected, which is the whole distinction the gate turns on.

Nothing is dropped and nothing is narrowed. No constraint: the six are required
*together*, and that is a statement about a verification being complete rather
than about a row being well-formed, so `release.is_verified` owns it. A CHECK
would also have to be satisfied by every historical row, which is exactly the
thing that must stay NULL.
"""
from alembic import op
import sqlalchemy as sa

revision = "e2b9d4c70a15"
down_revision = "a4f18c2d3b90"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("requirement_verified_inward", sa.Float()),
    ("requirement_verified_outward", sa.Float()),
    ("requirement_verified_unit", sa.String()),
    ("requirement_reference", sa.String()),
    ("requirement_verified_by", sa.String()),
    ("requirement_verified_at", sa.DateTime(timezone=True)),
)


def upgrade():
    for name, type_ in _COLUMNS:
        op.add_column("projects", sa.Column(name, type_, nullable=True))


def downgrade():
    for name, _type in reversed(_COLUMNS):
        op.drop_column("projects", name)
