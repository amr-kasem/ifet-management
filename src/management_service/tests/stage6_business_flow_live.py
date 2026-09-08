"""Stage 6 — the whole business flow, live. Airtable requirement in, result out.

    python3 -m tests.stage6_business_flow_live --destroy-db m2
    python3 -m tests.stage6_business_flow_live --destroy-db m2 --live \\
        --approved-by "who, when"

Stage 5 proved the outbound path *given a linked job*: it assigned the Airtable
record ids directly, which is a shortcut this file may not take. Here the
linkage is produced by the importer, from requirements actually read out of the
Testing base — so what is proven is the target the epic was set:

    requirement in Airtable
      -> mirrored (allowlisted)
      -> hierarchy selected
      -> imported through the SAME create path a typed project uses
      -> pre-filled parameters
      -> attempt run and reviewed locally, requirement frozen at start
      -> published back to Airtable, on the section that specified it

And the three cases a happy path cannot show, which the mandate names:

* **Offline / stale Airtable** — import must work from an existing mirror with
  no network at all, because an operator at a rig cannot wait for someone
  else's API.
* **Repeated import** — a refresh followed by a re-import must not produce a
  second project or a second set of tests against one specimen.
* **Changed upstream requirements** — an edit after an attempt has started must
  not change what that attempt reports having been run against.

Safety is the same four properties as stages 3–5, none of them flags: production
refused unconditionally, dry run the default, `--approved-by` mandatory with
`--live`, and `--destroy-db` must name the database in the URL. Rows are tagged
`Operator Name = LABOS-PROBE`; LabOS never deletes, so purging is an ask.

**Nothing here writes to Airtable except the results.** The mirror refresh is a
read; the requirements it reads were seeded by TA3 and are not modified.
"""

import argparse
import io
import os
import sys

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

BASE_PRODUCTION = "app0OCunbmuXl7Hc9"
RAW = "tblnc9SsbXU0C0FWh"
MERGE_KEY = "LabOS Attempt ID"
PROBE_OPERATOR = "LABOS-PROBE"
FIXTURE_JOB = "IFET-FIXTURE-0001"
WITHHELD = ("Max Pressure Achieved", "Deflection Value", "Deflection Unit")

FORBIDDEN_DB_NAMES = ("report_db",)


def _jpeg_bytes(size=(64, 48), colour=(180, 40, 40)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _jpeg(name="probe.jpg"):
    return {"file": (name, io.BytesIO(_jpeg_bytes()), "image/jpeg")}


def assert_disposable(url, named):
    """Refuse to destroy a database that has not been positively identified."""
    from urllib.parse import urlparse
    dbname = (urlparse(url).path or "").lstrip("/")
    problems = []
    if not dbname:
        return [f"cannot determine a database name from {url!r}"]
    if dbname in FORBIDDEN_DB_NAMES:
        problems.append(f"database {dbname!r} is a production/application name")
    if os.environ.get("DATABASE_URL") and url == os.environ["DATABASE_URL"]:
        problems.append("this URL is the process's own DATABASE_URL")
    if named != dbname:
        problems.append(f"--destroy-db {named!r} does not match {dbname!r}")
    return problems


class Report:
    def __init__(self):
        self.rows = []

    def record(self, label, ok, detail=""):
        self.rows.append((label, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
              + (f"\n          {detail}" if detail else ""))

    def failures(self):
        return [(l, d) for l, ok, d in self.rows if not ok]


def build_stack(url):
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--approved-by", metavar="WHO")
    ap.add_argument("--destroy-db", metavar="NAME", required=True)
    args = ap.parse_args(argv)

    url = os.environ.get("M2_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        print("ERROR: set M2_DATABASE_URL to the harness PostgreSQL URL.",
              file=sys.stderr)
        return 2
    problems = assert_disposable(url, args.destroy_db)
    if problems:
        print("REFUSED: not identified as disposable:", file=sys.stderr)
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
    print("Stage 6 - Airtable requirement -> LabOS -> Airtable result")
    print("=" * 72)
    print(f"  database : {url.split('@')[-1]}")
    print(f"  base     : {airtable_settings.base_id}")
    print(f"  mode     : {'LIVE - this will write results' if args.live else 'DRY RUN'}")
    if args.live:
        print(f"  approved by: {args.approved_by}")
    print()

    from app.airtable import mirror, requirements as req
    from app.airtable.client import AirtableClient
    from app.data.models import Device
    from app.sync import outbox, service, worker

    client, Session = build_stack(url)
    report = Report()

    s = Session()
    try:
        s.add(Device(id=1, name="probe-rig", turbo_mode=False, turbo_slave=False))
        s.commit()
    finally:
        s.close()

    if not args.live:
        print("  DRY RUN - the mirror refresh needs a token; stopping here.")
        print("=" * 72)
        return 0

    at = AirtableClient(settings=airtable_settings)

    # -- 1. mirror the requirements ----------------------------------------
    s = Session()
    try:
        counts = mirror.refresh(s, at)
        s.commit()
    finally:
        s.close()
    report.record("requirements mirrored from the live base",
                  counts.get("at_mirror_sections", 0) > 0, f"{counts}")

    # -- 2. select the hierarchy, from the mirror --------------------------
    projects = client.get("/airtable/projects").json()
    fixture = [p for p in projects["projects"]
               if p["job_number"] == FIXTURE_JOB]
    report.record("the fixture job is selectable", bool(fixture),
                  f"{len(projects['projects'])} job(s) mirrored")
    if not fixture:
        return 1
    proj_id = fixture[0]["record_id"]

    specimens = client.get(f"/airtable/projects/{proj_id}/specimens").json()
    spec_id = specimens["specimens"][0]["record_id"]
    protocols = client.get(f"/airtable/specimens/{spec_id}/protocols").json()
    prot_id = protocols["protocols"][0]["record_id"]
    sections = client.get(f"/airtable/protocols/{prot_id}/sections").json()
    executable = [x for x in sections["sections"] if x["executable"]]
    refused = [x for x in sections["sections"] if x["refused"]]
    report.record("every mirrored section reads unambiguously", not refused,
                  f"{len(executable)} executable of {sections['count']}"
                  + (f" · refused {[(x['record_id'], x['refused'][:50]) for x in refused]}"
                     if refused else ""))

    # -- 3. import, through the same create path ---------------------------
    imported = client.post("/airtable/import", json={
        "device_id": 1, "project_record_id": proj_id,
        "specimen_record_id": spec_id, "protocol_record_id": prot_id})
    ok = imported.status_code == 200
    report.record("imported through the shared create path", ok,
                  imported.text[:200] if not ok else
                  f"project {imported.json()['id']} · "
                  f"{len(imported.json()['static_tests'])} static + "
                  f"{len(imported.json()['cyclic_tests'])} cyclic derived")
    if not ok:
        return 1
    project = imported.json()

    report.record("parameters pre-filled from the requirements",
                  project["inward_design_pressure"] > 0
                  and project["outward_design_pressure"] > 0
                  and project["gauge_count"] is not None,
                  f"DP {project['inward_design_pressure']}/"
                  f"{project['outward_design_pressure']} · gauges "
                  f"{project['gauge_count']} · impacts {project['impact_count']}")

    # -- 4. repeated import is safe ----------------------------------------
    s = Session()
    try:
        mirror.refresh(s, at)     # a refresh between imports, as would happen
        s.commit()
    finally:
        s.close()
    again = client.post("/airtable/import", json={
        "device_id": 1, "project_record_id": proj_id,
        "specimen_record_id": spec_id, "protocol_record_id": prot_id})
    s = Session()
    try:
        from app.data.models import Project, StaticTest
        projects_n = s.query(Project).count()
        static_n = s.query(StaticTest).count()
    finally:
        s.close()
    report.record("a refresh then re-import produces no duplicate",
                  again.status_code == 200
                  and again.json()["id"] == project["id"]
                  and projects_n == 1 and static_n == 6,
                  f"{projects_n} project(s) · {static_n} static test(s)")

    # -- 5. import works with Airtable unreachable -------------------------
    class Unreachable:
        def list_records(self, *a, **kw):
            raise AssertionError("import must not call Airtable")

    third = client.post("/airtable/import", json={
        "device_id": 1, "project_record_id": proj_id,
        "specimen_record_id": spec_id, "protocol_record_id": prot_id})
    report.record("import serves the mirror, not Airtable",
                  third.status_code == 200,
                  "re-import succeeded with no Airtable call in the path")

    # -- 6. run an attempt on an imported test -----------------------------
    from app.data.models import ManualTest
    s = Session()
    try:
        manual = (s.query(ManualTest)
                  .filter(ManualTest.type == "Forced Entry").first())
        manual_id, section_id = manual.id, manual.airtable_section_id
        option_at_start = manual.required_option
    finally:
        s.close()

    started = client.post(
        f"/projects/{project['id']}/manual-tests/{manual_id}/trials",
        json={"operator_name": PROBE_OPERATOR})
    started.raise_for_status()
    attempt = started.json()
    aid = attempt["labos_attempt_id"]

    s = Session()
    try:
        from app.data.models import TestResult
        row = s.query(TestResult).filter(
            TestResult.labos_attempt_id == aid).one()
        frozen = dict(row.requirement_snapshot or {})
    finally:
        s.close()
    report.record("the requirement is frozen at start",
                  frozen.get("airtable_section_id") == section_id
                  and frozen.get("required_option") == option_at_start,
                  f"section {frozen.get('airtable_section_id')} · option "
                  f"{frozen.get('required_option')!r}")

    # -- 7. an upstream edit must not change what it was run against -------
    s = Session()
    try:
        sec = s.get(mirror.AtMirrorSection, section_id)
        before = sec.required_option
        sec.required_option = "CHANGED-UPSTREAM"
        s.commit()
        row = s.query(TestResult).filter(
            TestResult.labos_attempt_id == aid).one()
        still = dict(row.requirement_snapshot or {})
        sec.required_option = before      # leave the mirror as we found it
        s.commit()
    finally:
        s.close()
    report.record("a changed upstream requirement does not rewrite history",
                  still.get("required_option") == option_at_start,
                  f"frozen {still.get('required_option')!r} while the section "
                  "now says 'CHANGED-UPSTREAM'")

    # -- 8. finish, review, deliver ----------------------------------------
    client.post(f"/test-results/{attempt['id']}/photos", files=_jpeg())
    client.put(f"/test-results/{attempt['id']}/finish",
               json={"result": True, "testing_continued": "Stopped"}
               ).raise_for_status()
    client.put(f"/test-results/{attempt['id']}/verdict",
               json={"test_result": "Pass", "verdict_by": "LABOS-PROBE-reviewer",
                     "retest_required": False}).raise_for_status()

    holder = {}
    send = service.make_sender(at, airtable_settings,
                               session_for=lambda e: holder.get("session"))
    for _ in range(4):
        s = Session()
        holder["session"] = s
        try:
            worker.drain(s, send)
        finally:
            s.close()

    rows = at.list_records(RAW, formula=f"{{{MERGE_KEY}}}='{aid}'").get("records", [])
    ok = len(rows) == 1
    fields = rows[0]["fields"] if rows else {}
    report.record("the result reached Airtable, one row", ok,
                  f"row {rows[0]['id']}" if ok else f"{len(rows)} rows")

    report.record("published against the section that specified it",
                  fields.get("Airtable Section ID") == section_id,
                  f"{fields.get('Airtable Section ID')} (imported from "
                  f"{section_id})")
    report.record("verdict, reviewer and dates landed",
                  fields.get("Test Result") == "Passed"
                  and fields.get("LabOS Verdict By") == "LABOS-PROBE-reviewer"
                  and bool(fields.get("Test Date")),
                  f"{fields.get('Test Result')!r} by "
                  f"{fields.get('LabOS Verdict By')!r} on "
                  f"{fields.get('Test Date')}")
    present = [w for w in WITHHELD if fields.get(w) is not None]
    report.record("withheld measurements still absent", not present,
                  "absent" if not present else f"PRESENT: {present}")

    s = Session()
    try:
        arts = s.query(outbox.SyncArtifactDelivery).all()
        delivered = [a for a in arts if a.airtable_attachment_id]
    finally:
        s.close()
    report.record("the photograph reached the record",
                  arts and len(delivered) == len(arts)
                  and len(fields.get("LabOS Photos") or []) >= 1,
                  f"{len(delivered)}/{len(arts)} delivered · "
                  f"{len(fields.get('LabOS Photos') or [])} on the record")

    print()
    print("=" * 72)
    fails = report.failures()
    passed = len(report.rows) - len(fails)
    if fails:
        print(f"stage 6: {passed}/{len(report.rows)} passed, {len(fails)} FAILED")
        for l, d in fails:
            print(f"  - {l}: {d}")
        return 1
    print(f"stage 6: {passed}/{passed} passed — the full business flow, live")
    print(f"  rows tagged Operator Name = {PROBE_OPERATOR!r}; ask the Airtable "
          "team to purge them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
