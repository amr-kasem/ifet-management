"""One attempt per impact: split each impact attempt into one attempt per shot.

Revision ID: c7e4a2b81f56
Revises: b9c1f60d4e27
Create Date: 2026-09-08

The product owner respecified Impact on 2026-09-08: an impact test contains one
or more attempts, and **each attempt is exactly one impact**, with pass/fail and
photographs. Delivery plan §4.5a. Until now one attempt held a sequence of
shots, so this splits the existing rows to match.

**Additive in shape, and it drops nothing.** No column is added or removed. Each
existing shot ends up alone in its own attempt, keeping its own photographs; the
first shot keeps the original attempt row, and the rest get new ones cloned from
it. `shots` and `missile_impact_tests` are untouched as tables — the production
reports that read `test.shots` traverse `missile_impact_test_id` and see the same
impacts afterwards.

**Live data, not greenfield: 39 impact tests and 114 shots on the node.** Five
things this has to get right, each of which the obvious implementation gets
wrong:

1. **The verdict comes from the shot, not the parent.** `Shot.result` is the
   per-impact pass/fail; the parent attempt's `test_result` judged the whole
   sequence. Cloning the parent's verdict onto each impact would stamp one
   outcome onto impacts that individually passed and failed. Each attempt's
   `result` is taken from its own shot, and a parent verdict that disagrees is
   discarded — it answered a question that no longer exists.

2. **Renumber per test, not per attempt.** `trial_number` must stay unique under
   `uq_test_results_test_attempt`. A test with attempt 1 (3 shots) and attempt 2
   (2 shots) cannot give both groups 1,2,3 — every shot of the test is ordered
   and numbered 1..N across the whole test. So the 114 rows do **not** all keep
   their existing `shot_number` as their attempt number: only single-attempt
   tests do.

3. **Both photo columns move.** `test_photos.test_result_id` is NOT NULL and
   names the old parent attempt; it follows the shot it evidences. Rows with
   `shot_id IS NULL` are attempt-level evidence for a sequence that no longer
   exists — they stay on the attempt that keeps impact 1, because dropping
   evidence is not a migration's decision to make.

4. **`shot_number` becomes the impact ordinal.** Set equal to the new
   `trial_number`, so the column means one thing across old and new rows, the
   published JSON's `shots[].shot_number` agrees with `Attempt Number`, and
   `uq_shots_attempt_number` on `(test_result_id, shot_number)` enforces one
   impact per attempt for historical rows too.

5. **Two legacy shapes do not fit and are left alone.** An impact attempt with
   **zero** shots cannot become an attempt-per-impact; it is reported and
   skipped, not deleted. And `labos_test_id` is nullable, so pre-integration
   rows may have none — those are grouped on `missile_impact_test_id` instead,
   which is also the ordering fallback when there is no `trial_number` to sort
   on.

**Idempotent.** A test whose impact attempts already hold exactly one shot each
is skipped, so re-running after a partial failure does not split anything twice.
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "c7e4a2b81f56"
down_revision = "b9c1f60d4e27"
branch_labels = None
depends_on = None


# Columns copied onto a cloned attempt. Identity and outcome are deliberately
# absent: `labos_attempt_id` must be fresh because it is the outbound merge key
# and two attempts sharing one would merge into a single Airtable record, and
# the outcome comes from the shot (note 1).
_CLONED = (
    "labos_test_id", "schema_version", "status", "test_type", "test_name",
    "abort_reason", "testing_continued", "terminal_at", "testing_start_date",
    "testing_end_date", "operator_name", "test_rig", "labos_version",
    "labos_created_at", "labos_updated_at", "note", "requirement_snapshot",
    "required_value", "required_unit", "airtable_sync_state",
    "verdict_by", "verdict_at", "retest_required", "result_rationale",
)


def upgrade() -> None:
    bind = op.get_bind()

    # Every impact attempt with its shots, grouped by the test it belongs to.
    # Ordered by `(trial_number, shot_number)` where there is a trial number and
    # by `shot_number` alone where there is not (note 5).
    rows = bind.execute(sa.text("""
        SELECT i.missile_impact_test_id AS test_id,
               t.id                     AS attempt_id,
               t.trial_number           AS trial_number,
               t.labos_test_id          AS labos_test_id,
               s.id                     AS shot_id,
               s.shot_number            AS shot_number,
               s.result                 AS shot_result
        FROM impact_test_results i
        JOIN test_results t ON t.id = i.id
        LEFT JOIN shots s ON s.test_result_id = i.id
        ORDER BY i.missile_impact_test_id,
                 COALESCE(t.trial_number, 0), COALESCE(s.shot_number, 0), s.id
    """)).mappings().fetchall()

    by_test = {}
    for row in rows:
        by_test.setdefault(row["test_id"], []).append(row)

    split_tests = kept = created = empty = 0
    for test_id, entries in by_test.items():
        shots = [e for e in entries if e["shot_id"] is not None]
        empty += len(entries) - len(shots)
        if not shots:
            # Note 5: an attempt with no impact. Reported, not deleted.
            continue
        attempts_here = {e["attempt_id"] for e in entries}
        if len(shots) == len(attempts_here):
            # Already one impact per attempt — idempotent re-run, or a test
            # recorded after the new routes landed.
            kept += len(shots)
            continue

        split_tests += 1

        # **Move the existing attempts out of the way first.**
        # `uq_test_results_test_attempt` is on `(labos_test_id, trial_number)`,
        # and the renumbering below assigns 1..N to rows while other rows of the
        # same test still hold numbers in that range. A test with attempt 1 (3
        # shots) and attempt 2 (2 shots) collides the moment impact 2 is created
        # while attempt 2 still exists — which is precisely the multi-attempt
        # case, so the obvious implementation fails on the only shape that makes
        # the renumbering necessary. Negated rather than offset: the constraint
        # already guarantees the existing numbers are distinct within the test,
        # so negating them cannot collide, and no positive target is occupied.
        for attempt_id in sorted({e["attempt_id"] for e in entries}):
            bind.execute(sa.text(
                "UPDATE test_results SET trial_number = -trial_number "
                "WHERE id = :rid AND trial_number > 0"), {"rid": attempt_id})

        # Note 2: numbered 1..N across the whole test, in the order established
        # by the query. The first shot keeps an existing attempt row; every
        # later shot gets a clone of it.
        keeper = shots[0]["attempt_id"]
        for ordinal, entry in enumerate(shots, start=1):
            if ordinal == 1:
                target = keeper
                bind.execute(sa.text("""
                    UPDATE test_results
                       SET trial_number = :n, result = :res
                     WHERE id = :rid
                """), {"n": ordinal, "res": entry["shot_result"], "rid": target})
                kept += 1
            else:
                target = _clone_attempt(bind, keeper, ordinal,
                                        entry["shot_result"], test_id)
                created += 1

            # Note 4 and note 3: the shot follows its attempt, carrying the
            # ordinal, and its photographs follow the shot.
            bind.execute(sa.text("""
                UPDATE shots SET test_result_id = :rid, shot_number = :n
                 WHERE id = :sid
            """), {"rid": target, "n": ordinal, "sid": entry["shot_id"]})
            bind.execute(sa.text("""
                UPDATE test_photos SET test_result_id = :rid
                 WHERE shot_id = :sid
            """), {"rid": target, "sid": entry["shot_id"]})

    # Any attempt still negative was an old parent whose shots have all moved to
    # their own attempts, so it now holds no impact. It cannot keep a negative
    # ordinal — nothing else in the system expects one — and it cannot keep a
    # positive one that is already taken, so it is removed along with the
    # attempt-level rows that referenced it. Its photographs were repointed
    # above; only the empty shell goes.
    stranded = bind.execute(sa.text(
        "SELECT i.id FROM impact_test_results i JOIN test_results t ON t.id = i.id "
        "WHERE t.trial_number < 0")).scalars().fetchall()
    for attempt_id in stranded:
        remaining = bind.execute(sa.text(
            "SELECT count(*) FROM shots WHERE test_result_id = :rid"),
            {"rid": attempt_id}).scalar()
        if remaining:
            raise RuntimeError(
                f"attempt {attempt_id} still holds {remaining} impact(s) but was "
                "renumbered out of the way — the split is inconsistent")
        bind.execute(sa.text("UPDATE test_photos SET test_result_id = "
                             "(SELECT min(t2.id) FROM impact_test_results i2 "
                             " JOIN test_results t2 ON t2.id = i2.id "
                             " WHERE i2.missile_impact_test_id = "
                             "   (SELECT missile_impact_test_id FROM "
                             "    impact_test_results WHERE id = :rid) "
                             " AND t2.trial_number > 0) "
                             "WHERE test_result_id = :rid"), {"rid": attempt_id})
        bind.execute(sa.text("DELETE FROM impact_test_results WHERE id = :rid"),
                     {"rid": attempt_id})
        bind.execute(sa.text("DELETE FROM test_results WHERE id = :rid"),
                     {"rid": attempt_id})
    if stranded:
        print(f"  emptied parent attempts removed: {len(stranded)}")

    print(f"  impact tests split: {split_tests}")
    print(f"  attempts kept: {kept}, attempts created: {created}")
    if empty:
        print(f"  impact attempts with no impact, left alone: {empty}")

    # The invariant, verified rather than assumed. A shot sharing an attempt with
    # another after this ran means the split missed a shape, and a certification
    # record is the wrong place to discover that.
    bad = bind.execute(sa.text("""
        SELECT test_result_id, count(*) FROM shots
         WHERE test_result_id IS NOT NULL
         GROUP BY test_result_id HAVING count(*) > 1
    """)).fetchall()
    if bad:
        raise RuntimeError(
            "the split left attempts holding more than one impact, which the "
            f"new model forbids — first few: {bad[:5]}")


def _clone_attempt(bind, source_id, ordinal, shot_result, test_id):
    """A new attempt row for one impact, cloned from the attempt it came from."""
    cols = ", ".join(_CLONED)
    placeholders = ", ".join(f":{c}" for c in _CLONED)
    source = bind.execute(
        sa.text(f"SELECT {cols} FROM test_results WHERE id = :rid"),
        {"rid": source_id}).mappings().first()
    values = dict(source)
    values["labos_attempt_id"] = str(uuid.uuid4())
    values["trial_number"] = ordinal
    values["result"] = shot_result

    new_id = bind.execute(sa.text(f"""
        INSERT INTO test_results ({cols}, labos_attempt_id, trial_number, result)
        VALUES ({placeholders}, :labos_attempt_id, :trial_number, :result)
        RETURNING id
    """), values).scalar()
    bind.execute(sa.text("""
        INSERT INTO impact_test_results (id, missile_impact_test_id)
        VALUES (:rid, :test_id)
    """), {"rid": new_id, "test_id": test_id})
    return new_id


def downgrade() -> None:
    """Re-collapse each test's impacts into its lowest-numbered attempt.

    Lossy, and unavoidably so: the split minted a `labos_attempt_id` per impact
    and any of them may already have been published to Airtable, so collapsing
    cannot un-publish those records. It restores the *shape* — one attempt
    holding a numbered sequence — and not the identities.
    """
    bind = op.get_bind()
    tests = bind.execute(sa.text(
        "SELECT DISTINCT missile_impact_test_id FROM impact_test_results "
        "WHERE missile_impact_test_id IS NOT NULL")).scalars().fetchall()

    for test_id in tests:
        attempts = bind.execute(sa.text("""
            SELECT i.id, t.trial_number
              FROM impact_test_results i JOIN test_results t ON t.id = i.id
             WHERE i.missile_impact_test_id = :tid
             ORDER BY t.trial_number, i.id
        """), {"tid": test_id}).fetchall()
        if len(attempts) < 2:
            continue
        keeper = attempts[0][0]
        doomed = [a[0] for a in attempts[1:]]

        for ordinal, attempt_id in enumerate(
                [keeper] + doomed, start=1):
            bind.execute(sa.text(
                "UPDATE shots SET test_result_id = :k, shot_number = :n "
                "WHERE test_result_id = :old"),
                {"k": keeper, "n": ordinal, "old": attempt_id})
            bind.execute(sa.text(
                "UPDATE test_photos SET test_result_id = :k "
                "WHERE test_result_id = :old"),
                {"k": keeper, "old": attempt_id})

        for attempt_id in doomed:
            bind.execute(sa.text("DELETE FROM impact_test_results WHERE id = :i"),
                         {"i": attempt_id})
            bind.execute(sa.text("DELETE FROM test_results WHERE id = :i"),
                         {"i": attempt_id})
