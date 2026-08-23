"""Rehearse the P1 migration against a throwaway database.

    python3 tests/rehearse_p1_migration.py /tmp/rehearse.db

NOT part of the stdlib unittest suite — it needs SQLAlchemy and Alembic, which
`test_airtable_*.py` deliberately do not. Run it by hand before deploying a
schema change.

**Why this exists.** The test node has been offline since ~2026-07-24, so there
is no non-production database to try a migration on, and production holds 623
live attempt rows. This script stands in for that node: it builds the
pre-migration schema, seeds rows shaped like the real ones, runs `upgrade()`,
and asserts the properties the migration promises — including the one that
matters most, that historical attempts come out marked `Excluded` so the W4 sync
worker cannot upload every test IFET has ever performed.

The pre-migration schema below is transcribed from the **live production
database** (`\\d test_results`, `\\d projects` on management, 2026-08-23), not
from models.py — the whole point is to migrate what is actually there.
"""
import sys
import uuid
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

HERE = Path(__file__).resolve().parent.parent
MIGRATION = HERE / "alembic" / "versions" / "b7c2e9a41d38_p1_airtable_identity_and_attempts.py"


def load_migration():
    spec = importlib.util.spec_from_file_location("p1_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_pre_migration_schema(engine):
    """The production schema as it stands before this migration."""
    md = sa.MetaData()
    sa.Table("projects", md,
             sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String),
             sa.Column("parent_id", sa.Integer), sa.Column("device_id", sa.Integer),
             sa.Column("inward_design_pressure", sa.Float),
             sa.Column("outward_design_pressure", sa.Float))
    sa.Table("static_tests", md,
             sa.Column("id", sa.Integer, primary_key=True), sa.Column("index", sa.Integer),
             sa.Column("finished", sa.Boolean), sa.Column("project_id", sa.Integer))
    sa.Table("cyclic_tests", md,
             sa.Column("id", sa.Integer, primary_key=True), sa.Column("index", sa.Integer),
             sa.Column("finished", sa.Boolean), sa.Column("project_id", sa.Integer))
    sa.Table("test_results", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("trial_number", sa.Integer, nullable=False),
             sa.Column("result", sa.Boolean), sa.Column("note", sa.String),
             sa.Column("image_path", sa.String))
    sa.Table("static_test_results", md,
             sa.Column("id", sa.Integer, primary_key=True), sa.Column("static_test_id", sa.Integer))
    sa.Table("cyclic_test_results", md,
             sa.Column("id", sa.Integer, primary_key=True), sa.Column("cyclic_test_id", sa.Integer))
    md.create_all(engine)


def seed(engine):
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO projects (id,name,device_id,inward_design_pressure,"
                          "outward_design_pressure) VALUES (1,'90 Series SGD',1,60,60)"))
        c.execute(sa.text('INSERT INTO static_tests (id,"index",finished,project_id) VALUES (10,1,1,1)'))
        c.execute(sa.text('INSERT INTO cyclic_tests (id,"index",finished,project_id) VALUES (20,1,1,1)'))
        # two attempts at the SAME static test — they must share a labos_test_id
        for rid, trial, res in ((100, 1, 0), (101, 2, 1)):
            c.execute(sa.text("INSERT INTO test_results (id,trial_number,result,note) "
                              "VALUES (:i,:t,:r,'historic')"), {"i": rid, "t": trial, "r": res})
            c.execute(sa.text("INSERT INTO static_test_results (id,static_test_id) VALUES (:i,10)"),
                      {"i": rid})
        c.execute(sa.text("INSERT INTO test_results (id,trial_number,result,note) "
                          "VALUES (200,1,1,'historic cyclic')"))
        c.execute(sa.text("INSERT INTO cyclic_test_results (id,cyclic_test_id) VALUES (200,20)"))
        # an attempt with no subclass row — the base table permits it
        c.execute(sa.text("INSERT INTO test_results (id,trial_number,result,note) "
                          "VALUES (300,1,NULL,'orphan')"))


def check(engine, mig):
    failures = []
    with engine.connect() as c:
        rows = c.execute(sa.text(
            "SELECT id, labos_attempt_id, labos_test_id, airtable_sync_state, retest_required "
            "FROM test_results ORDER BY id")).fetchall()
        for r in rows:
            print(f"  id={r[0]:<4} attempt={str(r[1])[:8]}… test={str(r[2])[:8]}… "
                  f"sync={r[3]:<9} retest={r[4]}")

        by_id = {r[0]: r for r in rows}
        keys = [r[1] for r in rows]

        if any(k is None for k in keys):
            failures.append("an attempt has no merge key")
        if len(set(keys)) != len(keys):
            failures.append("merge keys are not unique")
        if by_id[100][2] != by_id[101][2]:
            failures.append("two attempts at the SAME test got different labos_test_id")
        if by_id[100][2] == by_id[200][2]:
            failures.append("static and cyclic tests collided on labos_test_id")
        if by_id[300][1] is None:
            failures.append("the orphan attempt got no merge key")
        if any(r[3] != "Excluded" for r in rows):
            failures.append("a historical row is not Excluded — W4 would upload it")
        if any(r[4] not in (0, False) for r in rows):
            failures.append("retest_required did not default to false")
        if c.execute(sa.text("SELECT count(*) FROM test_results WHERE status IS NOT NULL")).scalar():
            failures.append("migration invented lifecycle status for historical rows")
        if by_id[100][2] != str(uuid.uuid5(mig._NS, "static:10")):
            failures.append("labos_test_id is not deterministic")
    return failures


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    Path(path).unlink(missing_ok=True)
    engine = sa.create_engine("sqlite:///" + path)

    build_pre_migration_schema(engine)
    seed(engine)
    print("seeded 4 historical attempts (2 on one static test, 1 cyclic, 1 orphan)")

    mig = load_migration()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()
    print("upgrade() completed")

    failures = check(engine, mig)

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
    with engine.connect() as c:
        cols = {r[1] for r in c.execute(sa.text("PRAGMA table_info(test_results)")).fetchall()}
        left = cols & {"labos_attempt_id", "result_detail", "retest_required", "airtable_sync_state"}
        if left:
            failures.append(f"downgrade left columns behind: {left}")
        if c.execute(sa.text("SELECT count(*) FROM test_results")).scalar() != 4:
            failures.append("downgrade lost rows")
    print("downgrade() completed, rows intact")

    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL REHEARSAL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
