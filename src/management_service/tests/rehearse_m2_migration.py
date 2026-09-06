"""Rehearse P1 and M2 as ONE ordered upgrade, on real PostgreSQL.

    M2_DATABASE_URL=postgresql+psycopg2://... python3 tests/rehearse_m2_migration.py

Run it inside the harness:

    docker compose -f tests/postgres_harness/docker-compose.yaml \\
        run --rm tests python tests/rehearse_m2_migration.py

**Why a second rehearsal script.** `rehearse_p1_migration.py` runs against
SQLite, which was the only option when it was written and is not good enough
here for two reasons. Production is Postgres 13, and the properties M2 adds are
Postgres properties: a `NOT NULL DEFAULT 0` column, a named unique constraint the
savepoint retry collides against, and the composite index the claim query needs.
SQLite would accept all three and prove none of them.

**Why one upgrade and not two.** Production is still at `3a65a83e0463`, so P1 has
never been applied there. P1 and M2 will therefore arrive together, in order, in
a single `alembic upgrade head`. Rehearsing them separately would test a
sequence that will never happen; rehearsing them together is what the deploy
actually does.

The P1 head-check warning applies unchanged and applies to M2 too: confirm
`SELECT * FROM alembic_version;` immediately before deploying, because until the
startup.sh change ships every container restart still moves the node's head.
"""
import importlib.util
import os
import sys
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.rehearse_p1_migration import (            # noqa: E402
    build_pre_migration_schema,
    load_migration as load_p1,
)

HERE = Path(__file__).resolve().parent.parent
M2_MIGRATION = HERE / "alembic" / "versions" / "c4e1f8a92b07_m2_sync_outbox_and_fencing.py"

SYNC_TABLES = ("sync_outbox", "sync_attempt_state", "sync_state")


def seed(engine):
    """The pre-migration production shape, with Postgres-correct types.

    Deliberately not reusing `rehearse_p1_migration.seed`: it writes `1` and `0`
    into boolean columns, which SQLite accepts and Postgres refuses outright
    (`column "finished" is of type boolean but expression is of type integer`).
    That is itself a small argument for this script existing - the SQLite
    rehearsal was passing on inserts production would have rejected.
    """
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO projects (id,name,device_id,inward_design_pressure,"
            "outward_design_pressure) VALUES (1,'90 Series SGD',1,60,60)"))
        c.execute(sa.text(
            'INSERT INTO static_tests (id,"index",finished,project_id) '
            "VALUES (10,1,true,1)"))
        c.execute(sa.text(
            'INSERT INTO cyclic_tests (id,"index",finished,project_id) '
            "VALUES (20,1,true,1)"))
        # Two attempts at the SAME static test - they must share a labos_test_id.
        for rid, trial, res in ((100, 1, False), (101, 2, True)):
            c.execute(sa.text(
                "INSERT INTO test_results (id,trial_number,result,note) "
                "VALUES (:i,:t,:r,'historic')"), {"i": rid, "t": trial, "r": res})
            c.execute(sa.text(
                "INSERT INTO static_test_results (id,static_test_id) VALUES (:i,10)"),
                {"i": rid})
        c.execute(sa.text(
            "INSERT INTO test_results (id,trial_number,result,note) "
            "VALUES (200,1,true,'historic cyclic')"))
        c.execute(sa.text(
            "INSERT INTO cyclic_test_results (id,cyclic_test_id) VALUES (200,20)"))
        # An attempt with no subclass row - the base table permits it.
        c.execute(sa.text(
            "INSERT INTO test_results (id,trial_number,result,note) "
            "VALUES (300,1,NULL,'orphan')"))


def load_m2():
    spec = importlib.util.spec_from_file_location("m2_migration", M2_MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def apply(engine, migration, direction="upgrade"):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            getattr(migration, direction)()


def check_ordering(p1, m2):
    """M2 must descend from P1, or they are not an ordered pair at all."""
    failures = []
    if m2.down_revision != p1.revision:
        failures.append(
            f"M2 down_revision is {m2.down_revision!r}, expected P1's "
            f"{p1.revision!r} - they would apply as two heads, not one chain")
    return failures


def check_schema(engine):
    """The properties M2 promises, read back from the live catalogue."""
    failures = []
    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())

    for name in SYNC_TABLES:
        if name not in tables:
            failures.append(f"{name} was not created")
    if failures:
        return failures

    columns = {c["name"]: c for c in inspector.get_columns("sync_outbox")}

    epoch = columns.get("owner_epoch")
    if epoch is None:
        failures.append("sync_outbox.owner_epoch is missing - nothing can be fenced")
    else:
        if epoch["nullable"]:
            failures.append(
                "owner_epoch is nullable; every claim path would have to cope "
                "with a NULL epoch")
        default = str(epoch.get("default") or "")
        if "0" not in default:
            failures.append(f"owner_epoch has no server default of 0 (got {default!r})")

    # The FIFO invariant. Dropping it would not fix a collision, it would let two
    # phases of one attempt silently swap delivery order.
    uniques = {u["name"]: u for u in inspector.get_unique_constraints("sync_outbox")}
    if "uq_outbox_attempt_seq" not in uniques:
        failures.append("uq_outbox_attempt_seq is missing - the FIFO invariant is unenforced")
    else:
        cols = list(uniques["uq_outbox_attempt_seq"]["column_names"])
        if cols != ["attempt_id", "attempt_seq"]:
            failures.append(f"uq_outbox_attempt_seq covers {cols}, expected attempt_id+attempt_seq")

    indexes = {i["name"] for i in inspector.get_indexes("sync_outbox")}
    if "ix_sync_outbox_state_attempt_seq" not in indexes:
        failures.append(
            "the claim query's composite index is missing - every worker cycle "
            "would scan the whole outbox including delivered rows")

    if "attempt_id" not in {c["name"] for c in inspector.get_columns("sync_attempt_state")}:
        failures.append("sync_attempt_state has no attempt_id")

    return failures


def check_it_actually_works(engine):
    """Insert through the real code against the migrated schema.

    A migration that creates a table the application cannot use has not
    succeeded, and the catalogue alone will not tell you that.
    """
    from sqlalchemy.orm import sessionmaker

    from app.sync import outbox

    failures = []
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        outbox.enqueue(session, "REHEARSAL", outbox.CREATE, {"LabOS Attempt ID": "REHEARSAL"})
        outbox.enqueue(session, "REHEARSAL", outbox.TERMINAL, {"LabOS Attempt ID": "REHEARSAL"})
        session.commit()

        claimed = outbox.claim(session, now=None)
        session.commit()
        if len(claimed) != 1:
            failures.append(f"claim returned {len(claimed)} entries, expected 1 (the head)")
        elif claimed[0].owner_epoch != 1:
            failures.append(f"first claim gave epoch {claimed[0].owner_epoch}, expected 1")
        elif not outbox.owns(session, claimed[0], 1):
            failures.append("owns() disagreed with the epoch it was just given")

        # And the constraint really bites.
        duplicate = outbox.SyncOutbox(
            attempt_id="REHEARSAL", attempt_seq=1, phase=outbox.CREATE,
            payload={}, state=outbox.PENDING, attempts=0, owner_epoch=0)
        session.add(duplicate)
        try:
            session.commit()
            failures.append("a duplicate (attempt_id, attempt_seq) was accepted")
        except sa.exc.IntegrityError:
            session.rollback()
    finally:
        session.close()
    return failures


def main():
    url = os.environ.get("M2_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        print(__doc__)
        print("ERROR: set M2_DATABASE_URL to a PostgreSQL URL. This rehearsal is "
              "deliberately not runnable on SQLite.")
        return 2

    engine = sa.create_engine(url)
    p1, m2 = load_p1(), load_m2()

    failures = check_ordering(p1, m2)
    if failures:
        for f in failures:
            print("  FAIL", f)
        return 1
    print(f"ordering OK: {m2.revision} descends from {p1.revision}")

    # Start from nothing, so the rehearsal is repeatable.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    print("schema reset")

    build_pre_migration_schema(engine)
    seed(engine)
    print("seeded the pre-migration production shape (4 historical attempts)")

    apply(engine, p1, "upgrade")
    print(f"P1 {p1.revision} upgraded")
    apply(engine, m2, "upgrade")
    print(f"M2 {m2.revision} upgraded")

    failures = check_schema(engine)
    failures += check_it_actually_works(engine)

    # Down, in reverse, and the sync tables must be gone.
    apply(engine, m2, "downgrade")
    remaining = set(sa.inspect(engine).get_table_names()) & set(SYNC_TABLES)
    if remaining:
        failures.append(f"downgrade left {sorted(remaining)} behind")
    else:
        print("M2 downgraded cleanly")
    apply(engine, p1, "downgrade")
    print("P1 downgraded cleanly")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        return 1
    print("\nordered upgrade P1 -> M2 rehearsed, verified and rolled back: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
