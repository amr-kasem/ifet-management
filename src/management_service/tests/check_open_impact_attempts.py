#!/usr/bin/env python3
"""Pre-deploy check — are any Impact attempts open? **Read-only.**

    DATABASE_URL=postgresql://... python3 tests/check_open_impact_attempts.py

TA7a adds a completion gate: a finished Impact attempt must record the
classification and the target velocity it ran under. **Nothing is backfilled**
— the 39 tests already on the live node keep NULL in all three columns, and
nothing is inferred from their free-text `missile` or from historical shot
velocities. Historical *completed* attempts are untouched and stay untouched:
the gate fires at finish, and theirs already happened.

The one case that needs a human is an attempt that is **open at the moment of
deploy**. Its test has no classification, so finishing it under the new gate
will be refused until somebody sets the two values explicitly, or aborts it.
That is correct behaviour and a bad surprise, so it is asked before the
migration rather than discovered by an operator afterwards.

Exit codes: 0 none open, deploy normally · 1 some open, see the report and
coordinate · 2 could not check. A non-zero exit is **not** a failed
deployment — it is a deployment that needs a decision, and the output belongs
in the deployment evidence either way, including when the answer is zero.
"""
import os
import sys

import sqlalchemy as sa

QUERY = sa.text("""
    SELECT tr.id, tr.labos_attempt_id, tr.trial_number, tr.operator_name,
           tr.testing_start_date, t.id AS test_id, t.project_id, t.missile
    FROM test_results tr
    JOIN impact_test_results itr ON itr.id = tr.id
    JOIN missile_impact_tests t ON t.id = itr.missile_impact_test_id
    WHERE tr.status = 'In Progress'
    ORDER BY tr.id
""")


def main():
    url = os.getenv("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL is not set")
        return 2
    # Read-only by construction: one SELECT, no transaction that writes.
    try:
        engine = sa.create_engine(url)
        with engine.connect() as conn:
            rows = conn.execute(QUERY).fetchall()
            total = conn.execute(
                sa.text("SELECT count(*) FROM missile_impact_tests")).scalar()
    except Exception as exc:                                  # noqa: BLE001
        print(f"ERROR: could not query the database — {exc}")
        return 2

    print("PRE-DEPLOY CHECK — open Impact attempts (read-only)\n")
    print(f"  impact tests on this database: {total}")
    print(f"  open impact attempts:          {len(rows)}\n")

    if not rows:
        print("  None open. Deploy normally.")
        print("  Historical completed attempts are untouched and stay so.")
        return 0

    print("  OPEN ATTEMPTS — these cannot finish under the new gate until the")
    print("  classification and target velocity are set on their test:\n")
    print(f"  {'attempt':>8}  {'test':>6}  {'proj':>5}  {'#':>3}  "
          f"{'operator':<16} {'labos_attempt_id':<38} missile")
    for r in rows:
        print(f"  {r.id:>8}  {r.test_id:>6}  {r.project_id:>5}  "
              f"{r.trial_number:>3}  {str(r.operator_name or "-"):<16} "
              f"{str(r.labos_attempt_id or '-'):<38} {r.missile or '-'}")

    print(f"\n  {len(rows)} open. Do NOT infer the new values from the missile")
    print("  column or from historical velocities. Either set them explicitly")
    print("  on each test, abort the attempt, or coordinate the window around")
    print("  them. Record this output in the deployment evidence.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
