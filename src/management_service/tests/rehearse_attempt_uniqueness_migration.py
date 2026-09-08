"""Rehearse `e5f3a71c8d92` on a populated database — the full chain, then back.

    M2_DATABASE_URL=postgresql+psycopg2://... python -m tests.rehearse_attempt_uniqueness_migration

**Why a rehearsal rather than a unit test.** This migration rewrites
`labos_test_id` on rows that already exist and then adds a constraint over the
result, so the two halves can only be wrong *together*: normalising after
constraining would fail, and constraining data the normalisation was about to
change would pass for the wrong reason. Neither ordering error is visible
against an empty database, and the live node has 623 attempts.

It reuses the seed the manual-tests rehearsal established — the production
shape, `NOT NULL` where the node has it — then adds attempts of each of the
four parent types, deliberately carrying the **three formats the migration
exists to unify**: the P1 uuid5, a random uuid4, and the manual slug.

Chain: P1 -> M2 -> d1a6b93f2e57 -> e5f3a71c8d92 -> f7b2c04e19a5 ->
a3d8e5c71f04, then all six down.
"""

import os
import sys
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.rehearse_manual_tests_migration import (  # noqa: E402
    build_pre_migration_schema, load_mt, seed)
from tests.rehearse_m2_migration import load_m2                     # noqa: E402
from tests.rehearse_p1_migration import load_migration as load_p1    # noqa: E402

_NS = uuid.UUID("5f2b1c94-3a7e-4d18-9c60-1e8a7d2f4b03")


def _load(name, alias):
    import importlib.util
    path = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
            / name)
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_uq():
    return _load("e5f3a71c8d92_attempt_number_uniqueness.py", "uq_mig")


def load_artifacts():
    """`f7b2c04e19a5` — per-artifact delivery and durable publication failures."""
    return _load("f7b2c04e19a5_artifact_delivery.py", "artifact_mig")


def load_operator():
    """`a3d8e5c71f04` — operator identity captured at run start."""
    return _load("a3d8e5c71f04_run_start_operator.py", "operator_mig")


def check_artifact_tables(engine):
    """The two tables exist, keyed as the design requires, and bite.

    Both hold facts that were previously kept somewhere unable to express them —
    a photograph's delivery in a per-attempt sequence number, and a refused
    payload on a column nothing read. So the rehearsal checks the *keys*, not
    just the table names: the wrong key is exactly how each fact got lost.
    """
    failures = []
    insp = sa.inspect(engine)
    tables = set(insp.get_table_names())
    for name in ("sync_artifact_delivery", "sync_publication_failure"):
        if name not in tables:
            failures.append(f"{name} was not created")
    if failures:
        return failures

    pk = insp.get_pk_constraint("sync_artifact_delivery")["constrained_columns"]
    if pk != ["photo_id"]:
        failures.append(f"sync_artifact_delivery is keyed on {pk}, not the photo "
                        "— a per-attempt key is what discarded retried photos")

    uniques = {u["name"]: u["column_sorted"] if "column_sorted" in u
               else u["column_names"]
               for u in insp.get_unique_constraints("sync_publication_failure")}
    want = "uq_sync_publication_failure_attempt_phase"
    if want not in uniques:
        failures.append(f"{want} is missing — a second refusal of one phase "
                        "would pile up rows nobody reads")

    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO sync_publication_failure (attempt_id, phase, error) "
            "VALUES ('a1', 'terminal', 'first')"))
    try:
        with engine.begin() as c:
            c.execute(sa.text(
                "INSERT INTO sync_publication_failure (attempt_id, phase, error)"
                " VALUES ('a1', 'terminal', 'second')"))
        failures.append("two open failures for one (attempt, phase) were accepted")
    except sa.exc.IntegrityError:
        pass

    # `needs_reconciliation` must default false, not null: an artifact nobody has
    # flagged is not "unknown", it is fine.
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO sync_artifact_delivery (photo_id, attempt_id) "
            "VALUES (1, 'a1')"))
        flag = c.execute(sa.text("SELECT needs_reconciliation FROM "
                                 "sync_artifact_delivery WHERE photo_id=1")).scalar()
    if flag is not False:
        failures.append(f"needs_reconciliation defaulted to {flag!r}, not False")

    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM sync_artifact_delivery WHERE photo_id=1"))
        c.execute(sa.text("DELETE FROM sync_publication_failure WHERE attempt_id='a1'"))
    return failures


def check_operator_columns(engine):
    """All four test tables carry it — the mixin means a partial add breaks the ORM."""
    failures = []
    insp = sa.inspect(engine)
    for table in ("static_tests", "cyclic_tests", "manual_tests",
                  "missile_impact_tests"):
        cols = {c["name"] for c in insp.get_columns(table)}
        if "operator_name" not in cols:
            failures.append(f"{table} has no operator_name; the column is on the "
                            "AirtableProtocolRef mixin so every test table needs it")
    return failures


def apply(engine, mig, direction):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(mig, direction)()


def seed_attempts(engine):
    """Attempts of all four parent types, carrying all three id formats."""
    with engine.begin() as c:
        # The minimal pre-migration shape the shared builder creates — id,
        # index, finished, project_id — rather than today's models.py columns.
        # A rehearsal that seeded the current schema would be rehearsing
        # against a database the node does not have.
        c.execute(sa.text("INSERT INTO static_tests (id,index,finished,"
                          "project_id) VALUES (1,0,false,1)"))
        c.execute(sa.text("INSERT INTO cyclic_tests (id,index,finished,"
                          "project_id) VALUES (1,0,false,1)"))
        # `manual_tests` is created by d1a6b93f2e57, which has already run.
        c.execute(sa.text(
            "INSERT INTO manual_tests (id,project_id,type,required_option,"
            "finished) VALUES (1,1,'Forced Entry','Grade 40',false)"))

        rows = [
            # (test_results.id, child table, fk col, parent id, labos_test_id)
            # Static: already the derived form. Must be left alone.
            (101, "static_test_results", "static_test_id", 1,
             str(uuid.uuid5(_NS, "static:1"))),
            # Cyclic: a random uuid4 — the runtime path when no sibling existed.
            (102, "cyclic_test_results", "cyclic_test_id", 1, str(uuid.uuid4())),
            # Manual: the slug. The format that leaked a primary key.
            (103, "manual_test_results", "manual_test_id", 1, "forced-entry-1"),
            # Impact: the slug again, and a second attempt at the same test so
            # the constraint has a real (test, number) pair to protect.
            (104, "impact_test_results", "missile_impact_test_id", 1, "impact-1"),
            (105, "impact_test_results", "missile_impact_test_id", 1, "impact-1"),
        ]
        for n, (rid, child, fk, pid, tid) in enumerate(rows, start=1):
            c.execute(sa.text(
                "INSERT INTO test_results (id,trial_number,labos_attempt_id,"
                "labos_test_id,test_type,status) VALUES "
                "(:i,:n,:a,:t,'Forced Entry','Completed')"),
                {"i": rid, "n": 1 if rid != 105 else 2,
                 "a": f"attempt-{rid}", "t": tid})
            c.execute(sa.text(
                f"INSERT INTO {child} (id,{fk}) VALUES (:i,:p)"),
                {"i": rid, "p": pid})


def check(engine):
    failures = []
    insp = sa.inspect(engine)
    names = {u["name"] for u in insp.get_unique_constraints("test_results")}
    if "uq_test_results_test_attempt" not in names:
        failures.append("the uniqueness constraint was not created")

    with engine.begin() as c:
        got = dict(c.execute(sa.text(
            "SELECT id, labos_test_id FROM test_results ORDER BY id")).all())

    expect = {
        101: str(uuid.uuid5(_NS, "static:1")),
        102: str(uuid.uuid5(_NS, "cyclic:1")),
        103: str(uuid.uuid5(_NS, "manual:1")),
        104: str(uuid.uuid5(_NS, "impact:1")),
        105: str(uuid.uuid5(_NS, "impact:1")),
    }
    for rid, want in expect.items():
        if got.get(rid) != want:
            failures.append(f"row {rid}: labos_test_id is {got.get(rid)!r}, "
                            f"expected the derived {want!r}")
    if got.get(104) != got.get(105):
        failures.append("two attempts at one impact test ended in different groups")
    # The already-correct static row must not have been rewritten to something
    # else — determinism is the whole reason the backfill can be re-run.
    if got.get(101) != expect[101]:
        failures.append("an already-derived id was changed")

    # And the constraint must actually bite.
    with engine.begin() as c:
        c.execute(sa.text(
            "INSERT INTO test_results (id,trial_number,labos_attempt_id,"
            "labos_test_id,test_type,status) VALUES "
            "(201,9,'attempt-201','group-x','Forced Entry','Completed')"))
    try:
        with engine.begin() as c:
            c.execute(sa.text(
                "INSERT INTO test_results (id,trial_number,labos_attempt_id,"
                "labos_test_id,test_type,status) VALUES "
                "(202,9,'attempt-202','group-x','Forced Entry','Completed')"))
        failures.append("a duplicate (test, attempt number) was accepted")
    except sa.exc.IntegrityError:
        pass
    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM test_results WHERE id IN (201,202)"))
    return failures


def check_refuses_real_duplicates(engine, uq):
    """The migration must stop rather than renumber certification evidence."""
    with engine.begin() as c:
        c.execute(sa.text("ALTER TABLE test_results "
                          "DROP CONSTRAINT uq_test_results_test_attempt"))
        # Two attempts at ONE impact test sharing attempt number 1. After
        # normalisation they land in the same group, so this is a genuine
        # duplicate and not a format artefact.
        c.execute(sa.text(
            "INSERT INTO test_results (id,trial_number,labos_attempt_id,"
            "labos_test_id,test_type,status) VALUES "
            "(301,1,'attempt-301','impact-1','Forced Entry','Completed')"))
        c.execute(sa.text(
            "INSERT INTO impact_test_results (id,missile_impact_test_id) "
            "VALUES (301,1)"))
    try:
        apply(engine, uq, "upgrade")
        return ["the migration added the constraint over duplicated data"]
    except RuntimeError as exc:
        if "already duplicated" not in str(exc):
            return [f"refused for the wrong reason: {exc}"]
    finally:
        with engine.begin() as c:
            c.execute(sa.text("DELETE FROM impact_test_results WHERE id=301"))
            c.execute(sa.text("DELETE FROM test_results WHERE id=301"))
    return []


def main():
    url = os.environ.get("M2_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        print(__doc__)
        print("ERROR: set M2_DATABASE_URL to a PostgreSQL URL.")
        return 2

    engine = sa.create_engine(url)
    p1, m2, mt, uq = load_p1(), load_m2(), load_mt(), load_uq()
    art, op = load_artifacts(), load_operator()

    if art.down_revision != uq.revision:
        print(f"  FAIL {art.revision} revises {art.down_revision!r}, "
              f"not {uq.revision!r}")
        return 1
    if op.down_revision != art.revision:
        print(f"  FAIL {op.revision} revises {op.down_revision!r}, "
              f"not {art.revision!r}")
        return 1
    if uq.down_revision != mt.revision:
        print(f"  FAIL {uq.revision} revises {uq.down_revision!r}, "
              f"not {mt.revision!r}")
        return 1
    print(f"ordering OK: {p1.revision} -> {m2.revision} -> {mt.revision} "
          f"-> {uq.revision} -> {art.revision} -> {op.revision}")

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    build_pre_migration_schema(engine)
    seed(engine)
    for mig, label in ((p1, "P1"), (m2, "M2"), (mt, "manual-tests")):
        apply(engine, mig, "upgrade")
        print(f"{label} {mig.revision} upgraded")

    seed_attempts(engine)
    print("seeded 5 attempts across all four parent types, "
          "carrying all three labos_test_id formats")

    apply(engine, uq, "upgrade")
    print(f"attempt-uniqueness {uq.revision} upgraded")

    failures = check(engine)
    if not failures:
        print("all five ids normalised to the derived form; "
              "the constraint refuses a duplicate")
    failures += check_refuses_real_duplicates(engine, uq)
    if not failures:
        print("and it refuses to run at all over a genuine duplicate")

    apply(engine, uq, "upgrade")   # re-add after the refusal check removed it
    apply(engine, art, "upgrade")
    print(f"artifact-delivery {art.revision} upgraded")
    apply(engine, op, "upgrade")
    print(f"run-start-operator {op.revision} upgraded")
    failures += check_artifact_tables(engine)
    failures += check_operator_columns(engine)
    if not failures:
        print("artifact delivery keyed on the photograph, publication failures "
              "unique per (attempt, phase), operator_name on all four tables")

    for mig, label in ((op, "run-start-operator"), (art, "artifact-delivery"),
                       (uq, "attempt-uniqueness"), (mt, "manual-tests"),
                       (m2, "M2"), (p1, "P1")):
        apply(engine, mig, "downgrade")
        print(f"{label} downgraded")
    insp = sa.inspect(engine)
    names = {u["name"] for u in insp.get_unique_constraints("test_results")}
    if "uq_test_results_test_attempt" in names:
        failures.append("downgrade left the constraint behind")
    left = set(insp.get_table_names()) & {"sync_artifact_delivery",
                                          "sync_publication_failure"}
    if left:
        failures.append(f"downgrade left {sorted(left)} behind")

    print()
    if failures:
        for x in failures:
            print("  FAIL", x)
        return 1
    print("REHEARSAL PASSED — populated tables, forward and back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
