"""Rehearse P1 -> M2 -> manual-tests as ONE ordered upgrade, on real PostgreSQL.

    docker compose -f tests/postgres_harness/docker-compose.yaml \\
        run --rm tests python tests/rehearse_manual_tests_migration.py

Production is at `3a65a83e0463`, so all three arrive together in a single
`alembic upgrade head`. Rehearsing them separately would test a sequence that
will never happen.

**The point of this rehearsal is that the target is not empty.** Verified
read-only on the node 2026-09-08: 79 projects, 39 missile impact tests and 114
shots, with `missile`, `missile_weight`, `shots.area`, `shots.velocity` and
`shots.note` all `NOT NULL`. So the pre-migration shape seeded below carries
rows in exactly those columns, and the assertions check that they survive
untouched and that the widened columns then accept NULL. A migration that only
ever ran against an empty database would prove nothing about the 39 rows.
"""
import os
import sys
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.rehearse_p1_migration import load_migration as load_p1   # noqa: E402
from tests.rehearse_m2_migration import load_m2                     # noqa: E402

HERE = Path(__file__).resolve().parent.parent
MIGRATION = HERE / "alembic" / "versions" / "a3f7c21b9e04_manual_tests_and_evidence.py"
NUMBERED = HERE / "alembic" / "versions" / "b8d4e15a7c39_numbered_impacts_and_per_shot_photos.py"


def _load(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_mt():
    return _load(MIGRATION, "mt_mig")


def load_num():
    return _load(NUMBERED, "num_mig")


def build_pre_migration_schema(engine):
    """The live production shape, as read off the node on 2026-09-08.

    Only the tables this migration touches, plus the FK targets it needs. The
    NOT NULLs on missile/weight/area/velocity/note are the real ones.
    """
    md = sa.MetaData()
    sa.Table("project_parents", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String, nullable=False, unique=True))
    sa.Table("projects", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String, nullable=False),
             sa.Column("parent_id", sa.Integer, sa.ForeignKey("project_parents.id")),
             sa.Column("device_id", sa.Integer, nullable=False),
             sa.Column("inward_design_pressure", sa.Float, nullable=False),
             sa.Column("outward_design_pressure", sa.Float, nullable=False))
    sa.Table("static_tests", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("index", sa.Integer),
             sa.Column("finished", sa.Boolean),
             sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id")))
    sa.Table("cyclic_tests", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("index", sa.Integer),
             sa.Column("finished", sa.Boolean),
             sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id")))
    sa.Table("test_results", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("trial_number", sa.Integer, nullable=False),
             sa.Column("result", sa.Boolean), sa.Column("note", sa.String),
             sa.Column("image_path", sa.String))
    sa.Table("static_test_results", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("static_test_id", sa.Integer))
    sa.Table("cyclic_test_results", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("cyclic_test_id", sa.Integer))
    # The two that matter: populated, and NOT NULL where the node is NOT NULL.
    sa.Table("missile_impact_tests", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("missile", sa.String, nullable=False),
             sa.Column("missile_weight", sa.Float, nullable=False),
             sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id")))
    sa.Table("shots", md,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("area", sa.Float, nullable=False),
             sa.Column("velocity", sa.Float, nullable=False),
             sa.Column("result", sa.Boolean, nullable=False),
             sa.Column("note", sa.String, nullable=False),
             sa.Column("missile_impact_test_id", sa.Integer,
                       sa.ForeignKey("missile_impact_tests.id")))
    md.create_all(engine)


def seed(engine):
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO project_parents (id,name) VALUES (1,'IFET-26-0066')"))
        c.execute(sa.text("INSERT INTO projects (id,name,parent_id,device_id,"
                          "inward_design_pressure,outward_design_pressure) "
                          "VALUES (1,'90 Series SGD',1,1,60,60)"))
        c.execute(sa.text("INSERT INTO missile_impact_tests (id,missile,missile_weight,"
                          "project_id) VALUES (1,'Large Missile D',9.0,1)"))
        c.execute(sa.text("INSERT INTO missile_impact_tests (id,missile,"
                          "missile_weight,project_id) VALUES "
                          "(2,'Small Missile A',0.5,1)"))
        # Interleaved on purpose: a backfill that numbered globally rather than
        # per test would produce 1,3,4 for test 1 and 2 for test 2.
        for sid, tid, res in ((1, 1, True), (2, 2, True), (3, 1, False), (4, 1, True)):
            c.execute(sa.text("INSERT INTO shots (id,area,velocity,result,note,"
                              "missile_impact_test_id) VALUES "
                              "(:i,12.5,50.0,:r,'historic',:t)"),
                      {"i": sid, "r": res, "t": tid})


def apply(engine, mig, direction):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(mig, direction)()


def check_ordering(p1, m2, mt, num):
    f = []
    if m2.down_revision != p1.revision:
        f.append(f"M2 descends from {m2.down_revision}, expected {p1.revision}")
    if mt.down_revision != m2.revision:
        f.append(f"manual-tests descends from {mt.down_revision}, expected {m2.revision}")
    if num.down_revision != mt.revision:
        f.append(f"numbering descends from {num.down_revision}, expected {mt.revision}")
    return f


def check_backfill(engine):
    """The 114 production shots must come out numbered, in their own order.

    Seeded below as two impacts on test 1 and one on a second test, inserted
    out of id order relative to each other, so a backfill that numbered
    globally rather than per test would be caught.
    """
    f = []
    with engine.connect() as c:
        rows = c.execute(sa.text(
            "SELECT missile_impact_test_id, id, shot_number FROM shots "
            "ORDER BY missile_impact_test_id, id")).all()
        got = {}
        for test_id, _id, num in rows:
            got.setdefault(test_id, []).append(num)
        for test_id, nums in got.items():
            if nums != list(range(1, len(nums) + 1)):
                f.append(f"test {test_id} numbered {nums}, expected 1..{len(nums)}")
        if any(n is None for _, _, n in rows):
            f.append("a pre-existing shot was left unnumbered")

    # And the number must be unique within a test from here on.
    try:
        with engine.begin() as c:
            c.execute(sa.text(
                "INSERT INTO shots (id,shot_number,result,missile_impact_test_id) "
                "VALUES (900,1,true,1)"))
        f.append("two impacts numbered 1 on the same test were accepted")
    except sa.exc.IntegrityError:
        pass

    # A photograph may now belong to one impact.
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO test_photos (id,filename,path,shot_id,"
                          "missile_impact_test_id) VALUES "
                          "(50,'i1.jpg','uploads/i1.jpg',1,1)"))
    with engine.connect() as c:
        n = c.execute(sa.text("SELECT shot_id FROM test_photos WHERE id=50")).scalar()
        if n != 1:
            f.append("per-impact photograph did not attach")
    return f


def check_schema(engine):
    f = []
    insp = sa.inspect(engine)
    names = set(insp.get_table_names())
    for t in ("manual_tests", "test_photos"):
        if t not in names:
            f.append(f"{t} was not created")

    cols = {c["name"]: c for c in insp.get_columns("missile_impact_tests")}
    for n in ("labos_attempt_id", "attempt_number", "status", "test_result",
              "verdict_by", "verdict_at", "retest_required", "airtable_section_id"):
        if n not in cols:
            f.append(f"missile_impact_tests missing {n}")
    for n in ("missile", "missile_weight"):
        if not cols.get(n, {}).get("nullable"):
            f.append(f"missile_impact_tests.{n} should be nullable")

    scols = {c["name"]: c for c in insp.get_columns("shots")}
    for n in ("area", "velocity", "note"):
        if not scols.get(n, {}).get("nullable"):
            f.append(f"shots.{n} should be nullable")
    if scols.get("result", {}).get("nullable"):
        f.append("shots.result must stay NOT NULL — a shot without an outcome is not a shot")

    pcols = {c["name"] for c in insp.get_columns("projects")}
    for n in ("gauge_count", "impact_count", "airtable_meta",
              "airtable_project_id", "airtable_mockup_id"):
        if n not in pcols:
            f.append(f"projects missing {n}")
    if "airtable_project_id" not in {c["name"] for c in insp.get_columns("project_parents")}:
        f.append("project_parents missing airtable_project_id")
    return f


def check_live_rows_survived(engine):
    f = []
    with engine.connect() as c:
        row = c.execute(sa.text("SELECT missile, missile_weight FROM "
                                "missile_impact_tests WHERE id=1")).one()
        if row.missile != "Large Missile D" or row.missile_weight != 9.0:
            f.append(f"pre-existing impact row was altered: {row}")
        n = c.execute(sa.text("SELECT count(*) FROM shots")).scalar()
        if n != 4:
            f.append(f"expected 4 pre-existing shots, found {n}")
        # The widening must actually be usable, not merely declared.
        row = c.execute(sa.text("SELECT area, velocity, note FROM shots WHERE id=1")).one()
        if row.area != 12.5:
            f.append("existing shot lost its area")
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO shots (id,shot_number,result,"
                          "missile_impact_test_id) VALUES (99,99,true,1)"))
    with engine.connect() as c:
        row = c.execute(sa.text("SELECT area, velocity, note FROM shots WHERE id=99")).one()
        if (row.area, row.velocity, row.note) != (None, None, None):
            f.append("a pass/fail-only shot should be insertable with nothing else")
    return f


def check_manual_tests_work(engine):
    f = []
    with engine.begin() as c:
        for i, (typ, opt) in enumerate((("Forced Entry", "ASTM F588 Grade 40"),
                                        ("ANSI Z97.1", "Class A")), start=1):
            c.execute(sa.text(
                "INSERT INTO manual_tests (id,project_id,type,required_option,"
                "labos_attempt_id,attempt_number,status,test_result) VALUES "
                "(:i,1,:t,:o,:a,1,'In Progress','Pending')"),
                {"i": i, "t": typ, "o": opt, "a": f"uuid-{i}"})
    with engine.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM manual_tests")).scalar()
        if n != 2:
            f.append(f"expected 2 manual tests, found {n}")
    # The attempt id is the upsert key; a duplicate must be impossible.
    try:
        with engine.begin() as c:
            c.execute(sa.text(
                "INSERT INTO manual_tests (id,project_id,type,labos_attempt_id,"
                "attempt_number,status,test_result) VALUES "
                "(3,1,'Forced Entry','uuid-1',1,'In Progress','Pending')"))
        f.append("duplicate labos_attempt_id was accepted — the upsert key is not unique")
    except sa.exc.IntegrityError:
        pass
    # A photo must attach to either owner, and to neither by accident.
    with engine.begin() as c:
        c.execute(sa.text("INSERT INTO test_photos (id,filename,path,manual_test_id) "
                          "VALUES (1,'fe.jpg','uploads/fe.jpg',1)"))
        c.execute(sa.text("INSERT INTO test_photos (id,filename,path,"
                          "missile_impact_test_id) VALUES (2,'imp.jpg','uploads/imp.jpg',1)"))
    try:
        with engine.begin() as c:
            c.execute(sa.text("INSERT INTO test_photos (id,filename,path,manual_test_id) "
                              "VALUES (3,'x.jpg','uploads/x.jpg',999)"))
        f.append("a photo attached to a non-existent manual test — FK not enforced")
    except sa.exc.IntegrityError:
        pass
    return f


def main():
    url = os.environ.get("M2_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        print(__doc__)
        print("ERROR: set M2_DATABASE_URL to a PostgreSQL URL.")
        return 2

    engine = sa.create_engine(url)
    p1, m2, mt, num = load_p1(), load_m2(), load_mt(), load_num()

    failures = check_ordering(p1, m2, mt, num)
    if failures:
        for x in failures:
            print("  FAIL", x)
        return 1
    print(f"ordering OK: {p1.revision} -> {m2.revision} -> {mt.revision} "
          f"-> {num.revision}")

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    build_pre_migration_schema(engine)
    seed(engine)
    print("seeded the live production shape: 2 impact tests, 4 interleaved shots, "
          "NOT NULL as on the node")

    for mig, label in ((p1, "P1"), (m2, "M2"), (mt, "manual-tests"),
                       (num, "numbering")):
        apply(engine, mig, "upgrade")
        print(f"{label} {mig.revision} upgraded")

    failures = check_schema(engine)
    # Backfill first: it asserts the numbering is a contiguous 1..N per test,
    # and the checks below deliberately insert rows that are not.
    failures += check_backfill(engine)
    failures += check_live_rows_survived(engine)
    failures += check_manual_tests_work(engine)
    if not failures:
        print("schema, live-row survival and behaviour all verified")

    for mig, label in ((num, "numbering"), (mt, "manual-tests"),
                       (m2, "M2"), (p1, "P1")):
        apply(engine, mig, "downgrade")
        print(f"{label} downgraded")
    remaining = set(sa.inspect(engine).get_table_names()) & {"manual_tests", "test_photos"}
    if remaining:
        failures.append(f"downgrade left {sorted(remaining)} behind")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for x in failures:
            print("  -", x)
        return 1
    print("\nP1 -> M2 -> manual-tests -> numbering rehearsed against populated "
          "tables, verified and rolled back: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
