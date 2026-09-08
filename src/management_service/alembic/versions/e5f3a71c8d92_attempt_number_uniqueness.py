"""Attempt Number is unique within a test, and one LabOS Test ID format.

Revision ID: e5f3a71c8d92
Revises: d1a6b93f2e57
Create Date: 2026-09-08

Two related repairs, in one migration because neither is safe without the other.

**1. `UniqueConstraint(labos_test_id, trial_number)`.**

Attempt numbers were allocated `count()+1` / `len(trials)+1` with nothing
enforcing the result. Two starts at once — an operator double-pressing Start, or
a rig callback landing while the UI posts — produced two attempts numbered 2
under different `LabOS Attempt ID`s. Airtable would then hold two records that
read as one duplicated, and no roll-up could tell them apart.

`labos_test_id` is the correct left-hand column: it already *is* the identity of
"this test" and it lives on `test_results`. The per-subclass foreign keys live on
the child tables and cannot be constrained against a column here. `shots` has
carried exactly this shape on `(test_result_id, shot_number)` since
`d1a6b93f2e57`; this is the same rule one level up.

**2. Normalising `labos_test_id` to the derived form.**

Three writers produced three formats: this chain's own P1 backfill used a
deterministic `uuid5(_NS, "{kind}:{parent_id}")`, runtime static/cyclic minted a
random `uuid4` when no sibling existed, and the manual and impact routes used a
readable slug (`impact-7`). One Airtable column, three vocabularies — in the
field the Airtable team asked to use as their retest grouping key.

All five paths now derive the P1 form. This migration rewrites any row that does
not already match it, **before** the constraint is added, because two attempts
of one test carrying different test ids would otherwise satisfy the constraint
for the wrong reason: they would look like attempts at two different tests.

Ordering therefore matters: normalise, then constrain. The reverse would add a
constraint over data that is about to change under it.
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "e5f3a71c8d92"
down_revision = "d1a6b93f2e57"
branch_labels = None
depends_on = None

# Byte-identical to `b7c2e9a41d38._NS` and `app.data.attempts._NS`. All three
# must agree or a historical test and a new attempt at it land in different
# groups — the one thing `labos_test_id` exists to prevent.
_NS = uuid.UUID("5f2b1c94-3a7e-4d18-9c60-1e8a7d2f4b03")

CONSTRAINT = "uq_test_results_test_attempt"

# (child table, foreign key, kind token). The kind tokens for static and cyclic
# match what the P1 backfill derived, so those rows are already correct and the
# UPDATE below is a no-op for them — which is the point of a deterministic id.
_PARENTS = (
    ("static_test_results", "static_test_id", "static"),
    ("cyclic_test_results", "cyclic_test_id", "cyclic"),
    ("manual_test_results", "manual_test_id", "manual"),
    ("impact_test_results", "missile_impact_test_id", "impact"),
)


def _derived(kind, parent_id):
    return str(uuid.uuid5(_NS, f"{kind}:{parent_id}"))


def upgrade() -> None:
    bind = op.get_bind()

    # --- 1. normalise every existing labos_test_id to the derived form -------
    rewritten = 0
    for child, fk, kind in _PARENTS:
        rows = bind.execute(sa.text(
            f"SELECT c.id, c.{fk}, t.labos_test_id "
            f"FROM {child} c JOIN test_results t ON t.id = c.id"
        )).fetchall()
        for attempt_id, parent_id, current in rows:
            if parent_id is None:
                # An attempt with no parent cannot have a derived id. Leaving it
                # alone is correct: it is already excluded from sync, and
                # inventing a group for it would assert a test that is not there.
                continue
            want = _derived(kind, parent_id)
            if current != want:
                bind.execute(
                    sa.text("UPDATE test_results SET labos_test_id = :tid "
                            "WHERE id = :rid"),
                    {"tid": want, "rid": attempt_id},
                )
                rewritten += 1
    print(f"  labos_test_id normalised on {rewritten} row(s)")

    # --- 2. then constrain --------------------------------------------------
    # A duplicate at this point is real data corruption rather than a format
    # mismatch, so it must surface as a failed migration and not be silently
    # renumbered underneath a certification record.
    dupes = bind.execute(sa.text(
        "SELECT labos_test_id, trial_number, count(*) FROM test_results "
        "WHERE labos_test_id IS NOT NULL "
        "GROUP BY labos_test_id, trial_number HAVING count(*) > 1"
    )).fetchall()
    if dupes:
        raise RuntimeError(
            "cannot add the attempt-number constraint: "
            f"{len(dupes)} (test, attempt number) pair(s) are already duplicated. "
            "These need a human decision about which attempt is which — "
            f"first few: {dupes[:5]}"
        )

    op.create_unique_constraint(
        CONSTRAINT, "test_results", ["labos_test_id", "trial_number"])


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "test_results", type_="unique")
    # The normalisation is deliberately not reversed. The previous state was
    # three formats chosen by which code path happened to run, so there is no
    # single prior value to restore — and the derived id is valid under the old
    # schema anyway.
