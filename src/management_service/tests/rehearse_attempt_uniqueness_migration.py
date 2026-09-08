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
a3d8e5c71f04 -> b9c1f60d4e27, then all seven down.
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


def load_mirror():
    """`b9c1f60d4e27` — the Airtable mirror and the frozen requirement."""
    return _load("b9c1f60d4e27_mirror_and_requirement_freeze.py", "mirror_mig")


def load_split():
    """`c7e4a2b81f56` — one attempt per impact, splitting the existing rows."""
    return _load("c7e4a2b81f56_impact_one_attempt_per_impact.py", "split_mig")


def seed_legacy_impacts(engine):
    """The **old** impact shape: attempts holding sequences of shots.

    Four shapes, because the split gets each of them wrong differently:

    * test 91 — one attempt, three impacts, **mixed pass/fail**, with a parent
      verdict of `Fail` that must not be stamped onto the two that passed;
    * test 92 — **two** attempts, three impacts then two, which is the case that
      makes per-attempt renumbering wrong: both groups would claim 1,2,3;
    * test 93 — an attempt with **no impact at all**, which cannot become an
      attempt-per-impact and must survive untouched;
    * photographs on both columns — one per shot, and one attempt-level row with
      `shot_id IS NULL` for a sequence that is about to stop existing.
    """
    with engine.begin() as c:
        for tid in (91, 92, 93):
            c.execute(sa.text(
                "INSERT INTO missile_impact_tests (id,project_id,missile,"
                "missile_weight,finished) VALUES (:i,1,'Large Missile D',9.0,"
                "false)"), {"i": tid})

        # (attempt id, test id, trial_number, parent verdict, [(shot id, result)])
        plan = [
            (9001, 91, 1, "Fail", [(9101, True), (9102, False), (9103, True)]),
            (9002, 92, 1, "Pass", [(9104, True), (9105, True), (9106, True)]),
            (9003, 92, 2, "Fail", [(9107, False), (9108, True)]),
            (9004, 93, 1, "Pass", []),
        ]
        for rid, tid, trial, verdict, shots in plan:
            c.execute(sa.text(
                "INSERT INTO test_results (id,trial_number,labos_attempt_id,"
                "labos_test_id,test_type,status,test_result,result,"
                "operator_name,terminal_at) VALUES "
                "(:i,:n,:a,:t,'Impact','Completed',:v,:r,'technician-1',now())"),
                {"i": rid, "n": trial, "a": f"legacy-attempt-{rid}",
                 "t": str(uuid.uuid5(_NS, f"impact:{tid}")),
                 "v": verdict, "r": verdict == "Pass"})
            c.execute(sa.text(
                "INSERT INTO impact_test_results (id,missile_impact_test_id) "
                "VALUES (:i,:p)"), {"i": rid, "p": tid})
            for n, (sid, result) in enumerate(shots, start=1):
                c.execute(sa.text(
                    "INSERT INTO shots (id,shot_number,result,test_result_id,"
                    "missile_impact_test_id,area,velocity) VALUES "
                    "(:i,:n,:r,:a,:p,1.5,50.0)"),
                    {"i": sid, "n": n, "r": result, "a": rid, "p": tid})
                c.execute(sa.text(
                    "INSERT INTO test_photos (filename,path,test_result_id,"
                    "shot_id) VALUES (:f,:p,:a,:s)"),
                    {"f": f"impact-{sid}.jpg", "p": f"/uploads/impact-{sid}.jpg",
                     "a": rid, "s": sid})
        # Attempt-level evidence, for a sequence that is about to stop existing.
        c.execute(sa.text(
            "INSERT INTO test_photos (filename,path,test_result_id,shot_id) "
            "VALUES ('setup.jpg','/uploads/setup.jpg',9001,NULL)"))


def check_split(engine):
    """The five things §4.5a says the split has to get right."""
    failures = []
    with engine.connect() as c:
        # 1. one impact per attempt, everywhere.
        many = c.execute(sa.text(
            "SELECT test_result_id, count(*) FROM shots "
            "WHERE test_result_id IS NOT NULL GROUP BY test_result_id "
            "HAVING count(*) > 1")).fetchall()
        if many:
            failures.append(f"attempts still holding several impacts: {many}")

        # 2. renumbered across the test, not within the old attempt. Test 3 had
        #    3 shots then 2, so the five impacts must read 1..5 — the case that
        #    per-attempt numbering would have given 1,2,3,1,2.
        nums = c.execute(sa.text(
            "SELECT t.trial_number FROM impact_test_results i "
            "JOIN test_results t ON t.id = i.id "
            "WHERE i.missile_impact_test_id = 92 ORDER BY t.trial_number"
        )).scalars().fetchall()
        if list(nums) != [1, 2, 3, 4, 5]:
            failures.append(f"test 92 renumbered {list(nums)}, expected 1..5")

        # 3. `shot_number` mirrors `trial_number`, or the published JSON
        #    disagrees with Attempt Number and the constraint stops biting.
        mismatched = c.execute(sa.text(
            "SELECT s.id, s.shot_number, t.trial_number FROM shots s "
            "JOIN test_results t ON t.id = s.test_result_id "
            "WHERE s.shot_number <> t.trial_number")).fetchall()
        if mismatched:
            failures.append(f"shot_number does not mirror trial_number: {mismatched}")

        # 4. each attempt's outcome is its own impact's, not the parent's. Test
        #    2's parent said Fail; two of its three impacts passed.
        rows = c.execute(sa.text(
            "SELECT t.trial_number, t.result, s.result FROM impact_test_results i "
            "JOIN test_results t ON t.id = i.id "
            "JOIN shots s ON s.test_result_id = t.id "
            "WHERE i.missile_impact_test_id = 91 ORDER BY t.trial_number"
        )).fetchall()
        if [(r[1], r[2]) for r in rows] != [(True, True), (False, False),
                                            (True, True)]:
            failures.append(
                f"test 91 outcomes came from the parent, not the shots: {rows}")

        # 5. photographs followed their impact, and the attempt-level one stayed.
        orphans = c.execute(sa.text(
            "SELECT p.id FROM test_photos p JOIN shots s ON s.id = p.shot_id "
            "WHERE p.test_result_id <> s.test_result_id")).fetchall()
        if orphans:
            failures.append(f"photographs left on the wrong attempt: {orphans}")
        kept = c.execute(sa.text(
            "SELECT count(*) FROM test_photos WHERE shot_id IS NULL")).scalar()
        if kept != 1:
            failures.append(f"attempt-level evidence lost: {kept} row(s), expected 1")

        # The zero-impact attempt survives, untouched and still empty.
        empty = c.execute(sa.text(
            "SELECT count(*) FROM impact_test_results i "
            "JOIN test_results t ON t.id = i.id "
            "WHERE i.missile_impact_test_id = 93")).scalar()
        if empty != 1:
            failures.append(
                f"the impactless attempt was not left alone: {empty} row(s)")

        # Fresh merge keys: a clone sharing `labos_attempt_id` would merge two
        # impacts into one Airtable record.
        dupes = c.execute(sa.text(
            "SELECT labos_attempt_id, count(*) FROM test_results "
            "GROUP BY labos_attempt_id HAVING count(*) > 1")).fetchall()
        if dupes:
            failures.append(f"duplicate labos_attempt_id after the split: {dupes}")
    return failures


def check_split_is_idempotent(engine, split):
    """Re-running must not split anything twice."""
    with engine.connect() as c:
        before = c.execute(sa.text("SELECT count(*) FROM test_results")).scalar()
    apply(engine, split, "upgrade")
    with engine.connect() as c:
        after = c.execute(sa.text("SELECT count(*) FROM test_results")).scalar()
    if before != after:
        return [f"re-running the split changed the row count {before} -> {after}"]
    return []


def check_mirror(engine):
    """The mirror exists, and **has nowhere to put `Value`**.

    The absent column is the safety property, not an omission: `Value` is
    populated by a PDF extractor that drops blank cells, so a requirement of
    `+60/60` can arrive as `9` and a shifted value has already reached a record
    marked Passed (§10.19). A future migration that adds it would defeat the
    boundary, so the rehearsal asserts it stays absent.
    """
    failures = []
    insp = sa.inspect(engine)
    tables = set(insp.get_table_names())
    for name in ("at_mirror_projects", "at_mirror_specimens",
                 "at_mirror_protocols", "at_mirror_sections"):
        if name not in tables:
            failures.append(f"{name} was not created")
    if failures:
        return failures

    cols = {c["name"] for c in insp.get_columns("at_mirror_sections")}
    for forbidden in ("value", "Value"):
        if forbidden in cols:
            failures.append(
                f"at_mirror_sections has a {forbidden!r} column — the "
                "extractor's shifted column must have nowhere to land (§10.19)")
    # The typed requirement fields must all be there, or pre-fill reads nothing.
    for needed in ("requirement_code", "requirement_kind", "applicability",
                   "required_value_inward", "required_value_outward",
                   "required_unit", "required_option", "missile",
                   "missile_weight", "impact_velocity"):
        if needed not in cols:
            failures.append(f"at_mirror_sections has no {needed!r}")

    attempt_cols = {c["name"] for c in insp.get_columns("test_results")}
    if "requirement_snapshot" not in attempt_cols:
        failures.append("test_results has no requirement_snapshot, so an "
                        "attempt cannot freeze what it was run against")
    return failures


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
    art, op, mir = load_artifacts(), load_operator(), load_mirror()
    split = load_split()

    if art.down_revision != uq.revision:
        print(f"  FAIL {art.revision} revises {art.down_revision!r}, "
              f"not {uq.revision!r}")
        return 1
    if op.down_revision != art.revision:
        print(f"  FAIL {op.revision} revises {op.down_revision!r}, "
              f"not {art.revision!r}")
        return 1
    if split.down_revision != mir.revision:
        print(f"  FAIL {split.revision} revises {split.down_revision!r}, "
              f"not {mir.revision!r}")
        return 1
    if mir.down_revision != op.revision:
        print(f"  FAIL {mir.revision} revises {mir.down_revision!r}, "
              f"not {op.revision!r}")
        return 1
    if uq.down_revision != mt.revision:
        print(f"  FAIL {uq.revision} revises {uq.down_revision!r}, "
              f"not {mt.revision!r}")
        return 1
    print(f"ordering OK: {p1.revision} -> {m2.revision} -> {mt.revision} "
          f"-> {uq.revision} -> {art.revision} -> {op.revision} "
          f"-> {mir.revision} -> {split.revision}")

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
    apply(engine, mir, "upgrade")
    print(f"mirror-and-freeze {mir.revision} upgraded")
    failures += check_artifact_tables(engine)
    failures += check_operator_columns(engine)
    failures += check_mirror(engine)

    # --- one attempt per impact, over the legacy shape ---------------------
    seed_legacy_impacts(engine)
    print("seeded the old impact shape: 3 tests, 8 impacts across 4 attempts, "
          "one attempt with no impact, evidence on both photo columns")
    apply(engine, split, "upgrade")
    print(f"impact-split {split.revision} upgraded")
    split_failures = check_split(engine)
    failures += split_failures
    if not split_failures:
        print("each impact is its own attempt, renumbered across the test, "
              "outcomes taken from the impacts, evidence followed")
    failures += check_split_is_idempotent(engine, split)
    if not failures:
        print("artifact delivery keyed on the photograph, publication failures "
              "unique per (attempt, phase), operator_name on all four tables, "
              "mirror present with no column for `Value`")

    for mig, label in ((split, "impact-split"),
                       (mir, "mirror-and-freeze"), (op, "run-start-operator"),
                       (art, "artifact-delivery"),
                       (uq, "attempt-uniqueness"), (mt, "manual-tests"),
                       (m2, "M2"), (p1, "P1")):
        apply(engine, mig, "downgrade")
        print(f"{label} downgraded")
    insp = sa.inspect(engine)
    names = {u["name"] for u in insp.get_unique_constraints("test_results")}
    if "uq_test_results_test_attempt" in names:
        failures.append("downgrade left the constraint behind")
    left = set(insp.get_table_names()) & {
        "sync_artifact_delivery", "sync_publication_failure",
        "at_mirror_projects", "at_mirror_specimens", "at_mirror_protocols",
        "at_mirror_sections"}
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
