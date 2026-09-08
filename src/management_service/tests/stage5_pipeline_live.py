"""Stage 5 — the whole pipeline, live. Routes -> outbox -> real worker -> Airtable.

    python3 -m tests.stage5_pipeline_live                       # dry run (default)
    python3 -m tests.stage5_pipeline_live --live --approved-by "who, when"

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

REC = {"project": "recPROBE5project1", "mockup": "recPROBE5mockup01",
       "protocol": "recPROBE5protocol", "section": "recPROBE5section1"}

# (label, kind, creation body, whether it records impacts)
WORKFLOWS = (
    ("Forced Entry", "manual", {"type": "Forced Entry",
                                "required_option": "ASTM F588 Grade 40"}, False),
    ("ANSI Z97.1", "manual", {"type": "ANSI Z97.1",
                              "required_option": "Class A"}, False),
    ("Impact", "impact", {}, True),
)


def _jpeg(name="probe.jpg"):
    return {"file": (name, io.BytesIO(b"jpegbytes"), "image/jpeg")}


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
    """Real schema, real routes, real session — on the harness database."""
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


def link(Session, table, test_id):
    s = Session()
    try:
        s.execute(sa.text(
            f"UPDATE {table} SET airtable_protocol_id=:p, airtable_section_id=:x, "
            "airtable_section_name=:n WHERE id=:i"),
            {"p": REC["protocol"], "x": REC["section"],
             "n": "Probe section", "i": test_id})
        s.commit()
    finally:
        s.close()


def run_workflow(client, Session, kind, body, with_shots):
    """Through the real routes: create -> start -> evidence -> finish -> review."""
    path = "impact-tests" if kind == "impact" else "manual-tests"
    table = "missile_impact_tests" if kind == "impact" else "manual_tests"
    r = client.post(f"/projects/1/{path}/", json=body)
    r.raise_for_status()
    test_id = r.json()["id"]
    link(Session, table, test_id)

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
    args = ap.parse_args(argv)

    url = os.environ.get("M2_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        print("ERROR: set M2_DATABASE_URL to the harness PostgreSQL URL.",
              file=sys.stderr)
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
    for label, kind, body, with_shots in WORKFLOWS:
        test_id, attempt, path = run_workflow(client, Session, kind, body, with_shots)
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
        produced[label] = (test_id, attempt, path)

    if not args.live:
        print()
        print("  DRY RUN - the local half is proven; delivery needs --live.")
        print("=" * 72)
        f = report.failures()
        print(f"stage 5: {len(report.rows) - len(f)}/{len(report.rows)} local checks passed")
        return 1 if f else 0

    # -- delivery, with the production sender ------------------------------
    at = AirtableClient(settings=airtable_settings)
    send = service.make_sender(at, airtable_settings)
    s = Session()
    try:
        worker.drain(s, send)
    finally:
        s.close()
    print()

    for label, (test_id, attempt, path) in produced.items():
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
            and f.get("Airtable Section ID") == REC["section"]
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

    # -- retry: the same attempt must update the same row -------------------
    label, (test_id, attempt, path) = next(iter(produced.items()))
    aid = attempt["labos_attempt_id"]
    before = fetch(at, aid)[0]["id"]
    s = Session()
    try:
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
    s = Session()
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

    # -- attachments: the honest result ------------------------------------
    s = Session()
    try:
        parked = (s.query(outbox.SyncOutbox)
                  .filter(outbox.SyncOutbox.phase == "attachment",
                          outbox.SyncOutbox.state == outbox.PARKED).count())
        total = (s.query(outbox.SyncOutbox)
                 .filter(outbox.SyncOutbox.phase == "attachment").count())
        from app.sync import state as sync_state
        status = sync_state.status(s)
    finally:
        s.close()
    # **Not `parked == total`.** `worker.drain` stops when a cycle makes no
    # progress, and it claims at most one head per attempt per channel — so a
    # single drain parks the head of each attachment queue and leaves the rest
    # pending. That is correct: an attachment channel whose head will never
    # succeed should not be spun through on every cycle.
    #
    # What must hold is the property, not the count: every attachment that was
    # actually attempted parked with the uploader-missing reason, no RECORD
    # phase parked, and the headline status is not dragged to Retry Required by
    # a capability we have not built.
    attempted_ok = parked >= 1 and status["attachment_parked"] == parked
    channel_ok = status["parked"] == 0 and status["status"] != "Retry Required"
    report.record("attachments park visibly without pinning the status",
                  total > 0 and attempted_ok and channel_ok,
                  f"{parked} of {total} attachment entries parked (drain stops on "
                  f"no progress; the rest stay pending) · headline "
                  f"{status['status']!r} · record-channel parked="
                  f"{status['parked']} · attachment_parked="
                  f"{status['attachment_parked']}")

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
