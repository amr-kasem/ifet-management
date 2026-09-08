"""Stage 5 — the whole pipeline, live. Routes -> outbox -> real worker -> Airtable.

    python3 -m tests.stage5_pipeline_live --destroy-db m2
    python3 -m tests.stage5_pipeline_live --destroy-db m2 --live \
        --approved-by "who, when"

**What this proves that stage 4 does not.** Stage 4 built payloads with the
envelope and pushed them with the client: it tested the last two links of the
chain. This drives the chain end to end —

    HTTP route -> sync.publish -> sync_outbox -> worker.drain
               -> service.make_sender(AirtableClient) -> Airtable

— using the **actual worker** and the **actual production sender**, not
imitations of them. Every earlier suite injects a transport that accepts any
payload, which is right for testing the queue and is precisely how a sender that
would have rejected every photograph passed 270 tests.

Then it checks the three behaviours a single happy path cannot show:

* **Retry** — re-delivering the same attempt must update the same Airtable row.
* **Retest** — a second attempt must create a *separate* row, keep the same
  `LabOS Test ID`, and leave the first row untouched.
* **Withheld measurements** — `Max Pressure Achieved`, `Deflection Value` and
  `Deflection Unit` must be absent from the row that comes back.

Safety, and none of it is a flag:

1. **Production is refused unconditionally.**
2. **Dry run is the default** — the local half runs, nothing is sent.
3. **`--approved-by` is mandatory with `--live`** and echoed into the header.
4. Rows are tagged `Operator Name = LABOS-PROBE` with `probe5-` test ids.
   LabOS never deletes; purging is an ask for the Airtable team.
5. **`--destroy-db` is mandatory** and must name the database in the URL. This
   script drops and recreates every table, and it did so on any PostgreSQL URL
   including under dry run — refusing the production Airtable base does nothing
   to protect a database. See `assert_disposable`.

**Linkage here is synthetic and inserted directly**, which is the honest limit of
this layer: it proves the *outbound* path given a linked job. It does not prove
the importer, because the importer does not exist. Proving that requirements
entered in Airtable reach a rig is layer 3, and layer 3 may not shortcut the
linkage the way this file deliberately does.

No real equipment: rig observations are synthetic, which proves data handling
only. Calibration and hardware performance stay separate acceptance checks.
"""

import argparse
import io
import os
import sys
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

BASE_PRODUCTION = "app0OCunbmuXl7Hc9"
RAW = "tblnc9SsbXU0C0FWh"
MERGE_KEY = "LabOS Attempt ID"
PROBE_OPERATOR = "LABOS-PROBE"
WITHHELD = ("Max Pressure Achieved", "Deflection Value", "Deflection Unit")

# **Real records in the Testing base, not invented strings.** The fixture
# hierarchy `IFET-FIXTURE-0001` was seeded by TA3 and read back here on
# 2026-09-08. Fabricated ids (`recPROBE5project1`) proved only that Airtable
# preserves a string: a result carrying them belongs to no hierarchy, so nothing
# about linkage was actually demonstrated.
REC = {"project": "reclD9DwtosvMGSI3",     # IFET-FIXTURE-0001
       "mockup": "recclCv9R9AP5q9Vp",
       "protocol": "recPqpWwDunfuXuL5"}

# One Protocol Section per requirement code, so each test type links to the
# section that actually specifies it. Using one section for all five would
# publish a Cycles result against the Forced Entry requirement.
SECTIONS = {
    "STATIC_PRESSURE": "recvT3l042dXflKBF",   # DP (+) (PSF)
    "CYCLIC_PRESSURE": "recjHZf3kc6Nda3WU",   # Cyclic (PSF)
    "IMPACT_LMI": "recIx0KsuxNX4bUOF",        # LMI (impacts)
    "FORCED_ENTRY": "recVwSIEMXtezYa90",      # ASTM F588 Grade 40
    "ANSI_IMPACT": "reclwbuXbjg5v7IJQ",       # Class A
}

# (label, kind, requirement code, creation body, records impacts)
WORKFLOWS = (
    ("Static Load", "static", "STATIC_PRESSURE", {}, False),
    ("Cycles", "cyclic", "CYCLIC_PRESSURE", {}, False),
    ("Impact", "impact", "IMPACT_LMI", {}, True),
    ("Forced Entry", "manual", "FORCED_ENTRY",
     {"type": "Forced Entry", "required_option": "ASTM F588 Grade 40"}, False),
    ("ANSI Z97.1", "manual", "ANSI_IMPACT",
     {"type": "ANSI Z97.1", "required_option": "Class A"}, False),
)


def _jpeg(name="evidence.jpg"):
    """A **real** JPEG, not `b"jpegbytes"`.

    The uploader downscales every photograph before sending it, so a fixture
    that is not a decodable image tests the refusal path and nothing else. This
    was literal `b"jpegbytes"` until the uploader existed, which is why
    "attachments park" looked like isolation rather than a missing capability.
    """
    return {"file": (name, io.BytesIO(_jpeg_bytes()), "image/jpeg")}


def _jpeg_bytes(size=(64, 48), colour=(180, 40, 40)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


class Report:
    def __init__(self):
        self.rows = []

    def record(self, label, ok, detail=""):
        self.rows.append((label, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
              + (f"\n          {detail}" if detail else ""))

    def failures(self):
        return [(l, d) for l, ok, d in self.rows if not ok]


# Database names that must never be handed to `drop_all`, whatever else is set.
# `report_db` is what production and every developer compose file call it.
FORBIDDEN_DB_NAMES = ("report_db",)


def assert_disposable(url, named):
    """Refuse to destroy a database that has not been positively identified.

    **This guard was missing and the omission was dangerous.** `build_stack`
    calls `Base.metadata.drop_all()`, and it ran on **any** PostgreSQL URL —
    including under `--dry-run`, which reads as the safe mode. Refusing the
    production *Airtable base* protects nothing here: the destructive act is
    against the database.

    Positive identification, not absence of evidence:

    * the operator must name the database on the command line, and the name must
      match the one in the URL — so a stale `M2_DATABASE_URL` pointing somewhere
      unexpected fails rather than being wiped;
    * `report_db` is refused outright, under any name match;
    * a URL equal to the process's own `DATABASE_URL` is refused, because that
      is by construction the application's real database.
    """
    from urllib.parse import urlparse
    dbname = (urlparse(url).path or "").lstrip("/")
    if not dbname:
        return [f"cannot determine a database name from {url!r}"]
    problems = []
    if dbname in FORBIDDEN_DB_NAMES:
        problems.append(
            f"database {dbname!r} is a production/application name and is "
            "refused outright")
    if os.environ.get("DATABASE_URL") and url == os.environ["DATABASE_URL"]:
        problems.append(
            "this URL is the process's own DATABASE_URL — that is the "
            "application's database, not a disposable one")
    if named != dbname:
        problems.append(
            f"--destroy-db {named!r} does not match the database in the URL "
            f"({dbname!r}). Name the database you intend to destroy.")
    return problems


def build_stack(url):
    """Real schema, real routes, real session — on the harness database.

    Destructive: drops and recreates every table. Only reached after
    `assert_disposable` has passed.
    """
    from app import main
    from app.data.models import Base
    from fastapi.testclient import TestClient

    engine = sa.create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = _get_db
    return TestClient(main.app), Session


def seed(Session):
    from app.data.models import Device, Project, ProjectParent
    s = Session()
    try:
        s.add(Device(id=1, name="probe-rig", turbo_mode=False, turbo_slave=False))
        s.add(ProjectParent(id=1, name="IFET-PROBE5"))
        s.add(Project(id=1, name="Probe specimen", parent_id=1, device_id=1,
                      inward_design_pressure=60.0, outward_design_pressure=45.0,
                      airtable_project_id=REC["project"],
                      airtable_mockup_id=REC["mockup"]))
        s.commit()
    finally:
        s.close()


def link(Session, table, test_id, code, operator=None):
    """Bind a local test to the fixture's protocol and the section for `code`.

    Assigning the ids directly is this layer's deliberate shortcut — it proves
    the outbound path *given* a linked job. The ids themselves are real, so a
    published result genuinely attaches to the fixture hierarchy; what is not
    proven is the importer that would have set them, which does not exist.
    Layer 3 may not take this shortcut.
    """
    s = Session()
    try:
        s.execute(sa.text(
            f"UPDATE {table} SET airtable_protocol_id=:p, airtable_section_id=:x,"
            " airtable_section_name=:n, operator_name=:op WHERE id=:i"),
            {"p": REC["protocol"], "x": SECTIONS[code], "n": code,
             "op": operator, "i": test_id})
        s.commit()
    finally:
        s.close()


RIG_SQL = {
    "static": ("static_tests",
               "INSERT INTO static_tests (id, finished, index, pressure_factor,"
               " pressure, duration, type, preset, project_id) VALUES "
               "(:i, false, :idx, '0.75', 45.0, 10, 'static', false, 1)"),
    "cyclic": ("cyclic_tests",
               "INSERT INTO cyclic_tests (id, finished, index, type, cycles,"
               " low_pressure, high_pressure, resume, current_cycle, preset,"
               " project_id) VALUES "
               "(:i, false, :idx, 'cyclic', 1000, 30.0, 60.0, false, 640,"
               " false, 1)"),
}


def run_rig_workflow(client, Session, kind, code, index):
    """Static and Cycles: the rig posts a finished stage in one call.

    Included because stage 5 covered three of five types, and the two it omitted
    are the ones whose lifecycle was broken — an acceptance run over the manual
    types alone would have passed while Static and Cycles never queued a
    terminal at all.
    """
    table, sql = RIG_SQL[kind]
    s = Session()
    try:
        s.execute(sa.text(sql), {"i": index + 1, "idx": index})
        s.commit()
    finally:
        s.close()
    link(Session, table, index + 1, code, operator=PROBE_OPERATOR)

    path = "static_tests" if kind == "static" else "cyclic-tests"
    # Deliberately `deflections` alone — exactly what production firmware sends.
    # It completes because the operator was declared at run start.
    r = client.post(f"/projects/1/{path}/{index}/trials", json={
        "deflections": [{"deflection_gauge": "g1", "max_deflection": 1234.0,
                         "permanent_deflection": 12.0, "recovery": 60.0}]})
    r.raise_for_status()

    from app.data.models import TestResult
    s = Session()
    try:
        row = (s.query(TestResult)
               .order_by(TestResult.id.desc()).first())
        attempt = {"id": row.id, "labos_attempt_id": row.labos_attempt_id,
                   "labos_test_id": row.labos_test_id,
                   "trial_number": row.trial_number}
    finally:
        s.close()
    # A rig stage carries no operator review yet; give it one so the verdict
    # phase is exercised for these types too.
    client.put(f"/test-results/{attempt['id']}/verdict",
               json={"test_result": "Pass", "verdict_by": "LABOS-PROBE-reviewer",
                     "retest_required": False}).raise_for_status()
    return index + 1, attempt, path


def run_workflow(client, Session, kind, code, body, with_shots):
    """Through the real routes: create -> start -> evidence -> finish -> review."""
    path = "impact-tests" if kind == "impact" else "manual-tests"
    table = "missile_impact_tests" if kind == "impact" else "manual_tests"
    r = client.post(f"/projects/1/{path}/", json=body)
    r.raise_for_status()
    test_id = r.json()["id"]
    link(Session, table, test_id, code)

    a = client.post(f"/projects/1/{path}/{test_id}/trials",
                    json={"operator_name": PROBE_OPERATOR})
    a.raise_for_status()
    attempt = a.json()

    if with_shots:
        for i in range(3):
            sh = client.post(f"/test-results/{attempt['id']}/shots",
                             json={"result": True})
            sh.raise_for_status()
            client.post(f"/shots/{sh.json()['id']}/photos", files=_jpeg())
    else:
        client.post(f"/test-results/{attempt['id']}/photos", files=_jpeg())

    client.put(f"/test-results/{attempt['id']}/finish",
               json={"result": True, "testing_continued": "Stopped"}
               ).raise_for_status()
    client.put(f"/test-results/{attempt['id']}/verdict",
               json={"test_result": "Pass", "verdict_by": "LABOS-PROBE-reviewer",
                     "retest_required": False}).raise_for_status()
    return test_id, attempt, path


def fetch(client_at, attempt_id):
    rows = client_at.list_records(
        RAW, formula=f"{{{MERGE_KEY}}}='{attempt_id}'").get("records", [])
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--approved-by", metavar="WHO")
    ap.add_argument("--destroy-db", metavar="NAME", required=True,
                    help="the database this will DROP and recreate; must match "
                         "the database named in M2_DATABASE_URL")
    args = ap.parse_args(argv)

    url = os.environ.get("M2_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        print("ERROR: set M2_DATABASE_URL to the harness PostgreSQL URL.",
              file=sys.stderr)
        return 2

    # Before anything touches the schema, and before the dry-run branch —
    # `--dry-run` used to drop every table too.
    problems = assert_disposable(url, args.destroy_db)
    if problems:
        print("REFUSED: this would destroy a database that is not identified "
              "as disposable:", file=sys.stderr)
        for x in problems:
            print(f"  - {x}", file=sys.stderr)
        return 2

    from app.config import airtable_settings
    if airtable_settings.base_id == BASE_PRODUCTION:
        print(f"REFUSED: never writes production ({BASE_PRODUCTION}).",
              file=sys.stderr)
        return 2
    if args.live:
        if not airtable_settings.token:
            print("REFUSED: no AIRTABLE_TOKEN set.", file=sys.stderr)
            return 2
        if not args.approved_by:
            print('REFUSED: --live needs --approved-by "<who, and when>".',
                  file=sys.stderr)
            return 2

    print("=" * 72)
    print("Stage 5 - routes -> outbox -> real worker -> Airtable")
    print("=" * 72)
    print(f"  database : {url.split('@')[-1]}")
    print(f"  base     : {airtable_settings.base_id}")
    print(f"  mode     : {'LIVE - this will write' if args.live else 'DRY RUN'}")
    if args.live:
        print(f"  approved by: {args.approved_by}")
    print()

    from app.sync import outbox, service, worker
    from app.airtable.client import AirtableClient

    client, Session = build_stack(url)
    seed(Session)
    report = Report()

    # -- the local half: every workflow through the real routes -------------
    produced = {}
    for index, (label, kind, code, body, with_shots) in enumerate(WORKFLOWS):
        if kind in RIG_SQL:
            test_id, attempt, path = run_rig_workflow(
                client, Session, kind, code, index)
        else:
            test_id, attempt, path = run_workflow(
                client, Session, kind, code, body, with_shots)
        s = Session()
        try:
            entries = (s.query(outbox.SyncOutbox)
                       .filter(outbox.SyncOutbox.attempt_id
                               == attempt["labos_attempt_id"])
                       .order_by(outbox.SyncOutbox.attempt_seq).all())
            phases = [e.phase for e in entries]
        finally:
            s.close()
        record = [p for p in phases if p != "attachment"]
        attach = [p for p in phases if p == "attachment"]
        ok = record == ["create", "terminal", "verdict"]
        report.record(f"{label}: routes queued create/terminal/verdict", ok,
                      f"queued {record} + {len(attach)} attachment(s)")
        produced[label] = (test_id, attempt, path, code)

    if not args.live:
        print()
        print("  DRY RUN - the local half is proven; delivery needs --live.")
        print("=" * 72)
        f = report.failures()
        print(f"stage 5: {len(report.rows) - len(f)}/{len(report.rows)} local checks passed")
        return 1 if f else 0

    # -- delivery, with the production sender ------------------------------
    at = AirtableClient(settings=airtable_settings)
    holder = {}
    # The real production sender, with database access so it can deliver
    # attachments — the same function `service.main()` builds.
    send = service.make_sender(at, airtable_settings,
                               session_for=lambda entry: holder.get("session"))
    # Several cycles: an attachment defers until its attempt's create has
    # landed, so one drain is not enough by design.
    for _ in range(4):
        s = Session()
        holder["session"] = s
        try:
            worker.drain(s, send)
        finally:
            s.close()
    print()

    for label, (test_id, attempt, path, code) in produced.items():
        aid = attempt["labos_attempt_id"]
        rows = fetch(at, aid)
        if len(rows) != 1:
            report.record(f"{label}: one Airtable row per attempt", False,
                          f"found {len(rows)} rows")
            continue
        f = rows[0]["fields"]

        identity_ok = (
            f.get("Airtable Project ID") == REC["project"]
            and f.get("Airtable Mockup ID") == REC["mockup"]
            and f.get("Airtable Protocol ID") == REC["protocol"]
            # The section for THIS requirement code, not just any section.
            and f.get("Airtable Section ID") == SECTIONS[code]
            and f.get("LabOS Test ID") == attempt["labos_test_id"]
            and f.get(MERGE_KEY) == aid
            and f.get("Attempt Number") == attempt["trial_number"])
        report.record(f"{label}: identity round-trips", identity_ok,
                      f"row {rows[0]['id']} · attempt {f.get('Attempt Number')}")

        verdict_ok = (f.get("Test Result") == "Passed"
                      and f.get("Test Status") == "Completed"
                      and f.get("LabOS Verdict By") == "LABOS-PROBE-reviewer")
        report.record(f"{label}: verdict and reviewer landed", verdict_ok,
                      f"{f.get('Test Result')!r} by {f.get('LabOS Verdict By')!r}")

        times_ok = all(f.get(k) for k in
                       ("Testing Start Date", "Testing End Date", "Test Date"))
        report.record(f"{label}: timestamps present", times_ok,
                      f"start={f.get('Testing Start Date')} "
                      f"end={f.get('Testing End Date')}")

        present = [w for w in WITHHELD if f.get(w) is not None]
        report.record(f"{label}: withheld measurements absent", not present,
                      "absent" if not present else f"PRESENT: {present}")

        # Photographs, where the workflow produced any.
        s = Session()
        try:
            rows = (s.query(outbox.SyncArtifactDelivery)
                    .filter(outbox.SyncArtifactDelivery.attempt_id == aid).all())
            delivered = [r for r in rows if r.airtable_attachment_id]
            flagged = [r for r in rows if r.needs_reconciliation]
        finally:
            s.close()
        if rows:
            attached = f.get("LabOS Photos") or []
            ok = (len(delivered) == len(rows) and not flagged
                  and len(attached) >= len(rows))
            report.record(f"{label}: photographs attached to the record", ok,
                          f"{len(delivered)}/{len(rows)} delivered · "
                          f"{len(attached)} on the record · "
                          f"{len(flagged)} awaiting reconciliation")

    # -- retry: the same attempt must update the same row -------------------
    # Forced Entry explicitly, not "the first workflow": the first is now a rig
    # type whose retest is posted by the rig rather than started by an operator.
    label = "Forced Entry"
    test_id, attempt, path, code = produced[label]
    aid = attempt["labos_attempt_id"]
    before = fetch(at, aid)[0]["id"]
    s = Session()
    try:
        holder["session"] = s
        entry = (s.query(outbox.SyncOutbox)
                 .filter(outbox.SyncOutbox.attempt_id == aid,
                         outbox.SyncOutbox.phase == "verdict").first())
        resent = send(entry)
    finally:
        s.close()
    after = fetch(at, aid)
    report.record("retry: re-delivering an attempt updates the same row",
                  len(after) == 1 and after[0]["id"] == before == resent,
                  f"{before} -> {resent}, {len(after)} row(s)")

    # -- retest: a new attempt is a new row, same Test ID -------------------
    second = client.post(f"/projects/1/{path}/{test_id}/trials",
                         json={"operator_name": PROBE_OPERATOR})
    second.raise_for_status()
    second = second.json()
    client.put(f"/test-results/{second['id']}/finish",
               json={"result": False, "testing_continued": "Stopped"})
    for _ in range(3):
        s = Session()
        holder["session"] = s
        try:
            worker.drain(s, send)
        finally:
            s.close()

    rows2 = fetch(at, second["labos_attempt_id"])
    rows1 = fetch(at, aid)
    ok = (len(rows2) == 1 and len(rows1) == 1
          and rows2[0]["id"] != rows1[0]["id"]
          and rows2[0]["fields"].get("LabOS Test ID")
              == rows1[0]["fields"].get("LabOS Test ID")
          and rows2[0]["fields"].get("Attempt Number") == 2
          and rows1[0]["fields"].get("Test Result") == "Passed")
    report.record("retest: separate row, same Test ID, original preserved", ok,
                  f"attempt 1 {rows1[0]['id']} result="
                  f"{rows1[0]['fields'].get('Test Result')!r} · attempt 2 "
                  f"{rows2[0]['id']} number="
                  f"{rows2[0]['fields'].get('Attempt Number')}")

    # -- attachments: delivered, or the exact reason not ---------------------
    s = Session()
    holder["session"] = s
    try:
        entries = (s.query(outbox.SyncOutbox)
                   .filter(outbox.SyncOutbox.phase == "attachment").all())
        rows = s.query(outbox.SyncArtifactDelivery).all()
        delivered = [r for r in rows if r.airtable_attachment_id]
        flagged = [r for r in rows if r.needs_reconciliation]
        from app.sync import state as sync_state
        status = sync_state.status(s)
        states = {}
        errors = set()
        for e in entries:
            states[e.state] = states.get(e.state, 0) + 1
            if e.last_error:
                errors.add(e.last_error[:160])
    finally:
        s.close()

    ok = (entries and len(delivered) == len(entries) and not flagged)
    report.record(
        "photographs are delivered, not merely queued", ok,
        f"{len(delivered)}/{len(entries)} delivered · entry states {states} · "
        f"{len(flagged)} awaiting reconciliation · headline "
        f"{status['status']!r}" + (f" · errors: {sorted(errors)}" if errors else ""))

    print()
    print("=" * 72)
    fails = report.failures()
    passed = len(report.rows) - len(fails)
    if fails:
        print(f"stage 5: {passed}/{len(report.rows)} passed, {len(fails)} FAILED")
        for l, d in fails:
            print(f"  - {l}: {d}")
        return 1
    print(f"stage 5: {passed}/{passed} passed - the whole pipeline, live")
    print(f"  rows tagged Operator Name = {PROBE_OPERATOR!r}; ask the Airtable "
          "team to purge them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
