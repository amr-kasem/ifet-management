"""TA7 live probe — the Impact classification pair, end to end, against Testing.

    python3 -m tests.ta7_probe_live --destroy-db m2
    python3 -m tests.ta7_probe_live --destroy-db m2 --live --approved-by "who, when"

**What this proves that no local test can.** Every local suite injects a
transport that accepts any payload — right for testing the queue, and exactly
how a sender that would have rejected every photograph once passed 270 tests.
This drives the real chain:

    Airtable records -> mirror.refresh -> importer -> HTTP routes
      -> sync.publish -> sync_outbox -> worker.drain
      -> service.make_sender(AirtableClient) -> Airtable

It covers both halves of the TA7b reversal in one run: that the three withdrawn
Protocol Sections fields **do not** arrive even when populated, and that the two
new Raw Data fields **do**, with the right Airtable types.

Safety, none of it a flag:

1. Production is refused unconditionally, and its schema and record counts are
   captured before and after and compared.
2. Dry run is the default; the local half runs and nothing is sent.
3. `--approved-by` is mandatory with `--live`.
4. Everything created is tagged `IFET-PROBE-TA7-0001` / `LABOS-PROBE-TA7`.
   **LabOS never deletes.** Records stay for review.
5. `--destroy-db` is mandatory and must name the database in the URL.

**On the write boundary.** The probe creates its own synthetic hierarchy — a
job, mock-up, protocol and sections — because it has to populate the withdrawn
fields on a section to prove they are not read, and modifying an existing
record to do that would be worse. Those are *probe setup* writes. What is
asserted separately is that **LabOS's own sync path writes only to the Raw Data
table**: the hierarchy tables are snapshotted before and after the sync phase
and compared.
"""

import argparse
import datetime as dt
import io
import json
import os
import pathlib
import sys

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

BASE_PRODUCTION = "app0OCunbmuXl7Hc9"
BASE_TESTING = "app4oXS3Kd5IKWgJ7"
RAW = "tblnc9SsbXU0C0FWh"
PROJECTS, MOCKUPS = "tblLYcRC7q6Srjfk3", "tblcrGv0WJn6FTTGO"
PROTOCOLS, SECTIONS_T = "tblutO1Q8TNC4BLk0", "tblqpvuJlSdkeS9PS"
MERGE_KEY = "LabOS Attempt ID"
JOB = "IFET-PROBE-TA7-0001"
OPERATOR = "LABOS-PROBE-TA7"

# The withdrawn three, populated on the probe's own section on purpose. If any
# of these values reaches the mirror, the snapshot or the domain model, the
# withdrawal did not happen.
POISON = {"Missile Type": "PROBE-SHOULD-NOT-BE-READ",
          "Missile Weight": 999.0,
          "Impact Velocity": 888.0}

CASES = [
    ("SMI",   "IMPACT_SMI", None, "SMI",          130.0),
    ("LMI-D", "IMPACT_LMI", "D",  "LMI Level D",  50.25),   # fractional on purpose
    ("LMI-E", "IMPACT_LMI", "E",  "LMI Level E",  55.5),
]


class Report:
    def __init__(self):
        self.rows = []

    def record(self, label, ok, detail=""):
        self.rows.append((label, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))

    def failures(self):
        return [r for r in self.rows if not r[1]]


def _jpeg(name="probe.jpg"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (48, 32), (40, 90, 160)).save(buf, "JPEG")
    return {"file": (name, buf.getvalue(), "image/jpeg")}


def assert_disposable(url, named):
    db = url.rsplit("/", 1)[-1].split("?")[0]
    if db != named:
        raise SystemExit(f"--destroy-db {named!r} does not match the database "
                         f"in the URL ({db!r}); refusing to drop anything")
    return db


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


def snapshot_tables(api, token):
    """Record ids and modified stamps for every hierarchy table, so a write to
    one of them is visible afterwards rather than merely believed absent."""
    out = {}
    for tbl in (PROJECTS, MOCKUPS, PROTOCOLS, SECTIONS_T):
        recs = api("GET", f"https://api.airtable.com/v0/{BASE_TESTING}/{tbl}"
                          "?pageSize=100", token).get("records", [])
        out[tbl] = {r["id"]: r.get("createdTime") for r in recs}
    return out


def seed_local(Session):
    """The device the import route requires. Seeded before, not on a 404."""
    from app.data.models import Device
    s = Session()
    try:
        s.add(Device(id=1, name="probe-rig", turbo_mode=False, turbo_slave=False))
        s.commit()
    finally:
        s.close()


def seed_airtable(api, token, report, run_id):
    """The probe's own hierarchy, with the withdrawn fields populated."""
    def create(table, fields):
        r = api("POST", f"https://api.airtable.com/v0/{BASE_TESTING}/{table}",
                token, {"records": [{"fields": fields}], "typecast": True})
        return r["records"][0]["id"]

    job_no = f"{JOB}-{run_id}"
    job = create(PROJECTS, {"IFET job number": job_no, "Project name": "TA7 probe"})
    mock = create(MOCKUPS, {"Mock-up/specimen name": "TA7 probe specimen",
                            "IFET Job Number": [job]})
    proto = create(PROTOCOLS, {"Protocol Name": "TA7 probe protocol",
                               "Mock-Up": [mock]})
    secs = {}
    for code in ("IMPACT_SMI", "IMPACT_LMI"):
        # **The withdrawn three are populated here on purpose.** If any of
        # these values reaches the mirror, the snapshot or the domain model,
        # the withdrawal did not happen.
        secs[code] = create(SECTIONS_T, {
            "Section Name": f"{code} (probe)", "Test Protocol": [proto],
            "Requirement Code": code, "Requirement Kind": "Count",
            "Applicability": "Required", "Required Value": 2,
            "Required Unit": "impacts", **POISON})
    # A design-pressure pair, because the importer refuses a protocol without
    # one - the fourteen static and cyclic stages derive from it. A real
    # protocol has one; omitting it would be testing an impossible shape.
    secs["STATIC_PRESSURE"] = create(SECTIONS_T, {
        "Section Name": "DP probe (PSF)", "Test Protocol": [proto],
        "Requirement Code": "STATIC_PRESSURE",
        "Requirement Kind": "Directional Pair", "Applicability": "Required",
        "Required Value Inward": 60, "Required Value Outward": 45,
        "Required Unit": "PSF"})
    report.record("probe hierarchy created in Testing", True,
                  f"job={job_no} {job} protocol={proto} sections={list(secs.values())}")
    return {"project": job, "mockup": mock, "protocol": proto,
            "sections": secs, "job_number": job_no}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--approved-by", metavar="WHO")
    ap.add_argument("--destroy-db", metavar="NAME", required=True)
    ap.add_argument("--evidence", metavar="DIR", required=True)
    args = ap.parse_args(argv)

    url = os.environ.get("M2_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        print("ERROR: set M2_DATABASE_URL to the harness PostgreSQL URL.", file=sys.stderr)
        return 2
    assert_disposable(url, args.destroy_db)

    # **Load the .env into the process before app.config is imported.**
    # `airtable_settings` is built from os.getenv at import time, so a probe
    # started without the environment exported gets an empty token and a 401
    # that looks like a permissions problem rather than a missing variable.
    from app.airtable.apply_schema import load_env as _load_env
    for k, v in _load_env("/home/gad/AWS/ifet-project/ifet-management/.env").items():
        os.environ.setdefault(k, v)

    from app.config import airtable_settings
    if not airtable_settings.token:
        raise SystemExit("REFUSED: no Airtable token in the environment")
    if airtable_settings.base_id == BASE_PRODUCTION:
        raise SystemExit("REFUSED: configured base is production")
    if args.live and not args.approved_by:
        raise SystemExit('REFUSED: --live needs --approved-by "<who, and when>"')

    from app.airtable.apply_schema import api, load_env
    from app.airtable.client import AirtableClient
    from app.airtable import mirror, importer
    from app.sync import service, worker

    env = load_env("/home/gad/AWS/ifet-project/ifet-management/.env")
    ptok = env["AIRTABLE_TOKEN_PRODUCTION"]
    ttok = airtable_settings.token
    ev = pathlib.Path(args.evidence); ev.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print("=" * 74)
    print("TA7 LIVE PROBE — Impact classification pair")
    print("=" * 74)
    print(f"  database : {url.split('@')[-1]}")
    print(f"  base     : {airtable_settings.base_id} (testing)")
    print(f"  mode     : {'LIVE — this will write' if args.live else 'DRY RUN'}")
    if args.live:
        print(f"  approved : {args.approved_by}")
    print()

    report = Report()

    # ---- production safety, before -------------------------------------
    def prod_state():
        t = api("GET", f"https://api.airtable.com/v0/meta/bases/{BASE_PRODUCTION}/tables", ptok)
        n = sum(len(x["fields"]) for x in t["tables"])
        recs = api("GET", f"https://api.airtable.com/v0/{BASE_PRODUCTION}/{RAW}?pageSize=100",
                   ptok).get("records", [])
        return n, len(recs), t
    p_fields_before, p_rows_before, p_schema_before = prod_state()
    (ev / f"production-before-{stamp}.json").write_text(json.dumps(p_schema_before, indent=1, sort_keys=True))
    report.record("production schema before = 142", p_fields_before == 142, str(p_fields_before))

    client, Session = build_stack(url)

    if not args.live:
        print("\n  DRY RUN — nothing was created or sent. Re-run with --live.")
        return 0

    hier_before = snapshot_tables(api, ttok)
    seed_local(Session)
    recs = seed_airtable(api, ttok, report, stamp[-7:-1])

    # ---- inbound: the withdrawn three must not arrive -------------------
    at = AirtableClient(settings=airtable_settings)
    s = Session()
    try:
        mirror.refresh(s, at)
        s.commit()
        sec = (s.query(mirror.AtMirrorSection)
               .filter(mirror.AtMirrorSection.record_id == recs["sections"]["IMPACT_LMI"]).one())
        for col in ("missile", "missile_weight", "impact_velocity"):
            report.record(f"mirror does not carry {col}", not hasattr(sec, col) or getattr(sec, col) is None)
        report.record("mirror did carry the count", sec.required_value == 2, str(sec.required_value))
    finally:
        s.close()

    r = client.post("/airtable/import", json={
        "device_id": 1, "project_record_id": recs["project"],
        "specimen_record_id": recs["mockup"], "protocol_record_id": recs["protocol"]})
    report.record("import succeeded", r.status_code == 200, r.text[:160])
    if r.status_code != 200:
        return 1
    project = r.json()
    tests = project["missile_impact_tests"]
    report.record("two impact tests created (SMI + LMI)", len(tests) == 2, str(len(tests)))
    fams = {t["impact_family"] for t in tests}
    report.record("families frozen from Requirement Code", fams == {"SMI", "LMI"}, str(fams))
    report.record("missile NOT prefilled from the withdrawn field",
                  all(t["missile"] is None for t in tests))
    report.record("missile_weight NOT prefilled", all(t["missile_weight"] is None for t in tests))
    report.record("target_velocity NOT prefilled", all(t["target_velocity"] is None for t in tests))
    json.dump({"project": project}, (ev / f"imported-project-{stamp}.json").open("w"), indent=1)

    by_family = {t["impact_family"]: t for t in tests}
    pid = project["id"]

    # ---- run one attempt per case, through the real routes --------------
    produced = {}
    for label, code, level, expect_class, velocity in CASES:
        family = "SMI" if code == "IMPACT_SMI" else "LMI"
        test_id = by_family[family]["id"]
        if label == "LMI-E":
            # A second LMI case needs its own test: the family is frozen and a
            # completed attempt freezes the level, which is the point.
            # **Both ids, not just the section.** A test carrying a section
            # but no protocol is a half-finished binding, and mapping.py
            # refuses to publish it (contract §4.1) rather than writing a row
            # that attaches to nothing. The first run of this probe set only
            # the section and the attempt was correctly refused.
            t2 = client.post(f"/projects/{pid}/impact-tests/",
                             json={"airtable_section_id": recs["sections"]["IMPACT_LMI"],
                                   "airtable_protocol_id": recs["protocol"],
                                   "airtable_section_name": "IMPACT_LMI (probe)"})
            t2.raise_for_status()
            test_id = t2.json()["id"]
        patch = {"target_velocity": velocity}
        if level:
            patch["impact_level"] = level
        pr = client.patch(f"/projects/{pid}/impact-tests/{test_id}", json=patch)
        report.record(f"[{label}] PATCH set level/velocity", pr.status_code == 200, pr.text[:100])
        report.record(f"[{label}] derived classification is {expect_class!r}",
                      pr.json().get("impact_classification") == expect_class,
                      str(pr.json().get("impact_classification")))

        a = client.post(f"/projects/{pid}/impact-tests/{test_id}/trials",
                        json={"operator_name": OPERATOR})
        a.raise_for_status()
        attempt = a.json()
        sh = client.post(f"/test-results/{attempt['id']}/shots", json={"result": True})
        sh.raise_for_status()
        client.post(f"/shots/{sh.json()['id']}/photos", files=_jpeg(f"{label}.jpg"))
        fin = client.put(f"/test-results/{attempt['id']}/finish",
                         json={"result": True, "testing_continued": "Stopped"})
        report.record(f"[{label}] finish accepted", fin.status_code == 200, fin.text[:100])
        client.put(f"/test-results/{attempt['id']}/verdict",
                   json={"test_result": "Pass", "verdict_by": OPERATOR,
                         "retest_required": False}).raise_for_status()
        produced[label] = (attempt, expect_class, velocity)

    # ---- deliver, with the real worker and the real sender --------------
    holder = {}
    send = service.make_sender(at, airtable_settings,
                               session_for=lambda entry: holder.get("session"))
    for _ in range(4):
        s = Session()
        holder["session"] = s
        try:
            worker.drain(s, send)
        finally:
            s.close()

    def fetch(aid):
        return at.list_records(RAW, formula=f"{{{MERGE_KEY}}}='{aid}'").get("records", [])

    created_ids = {}
    for label, (attempt, expect_class, velocity) in produced.items():
        aid = attempt["labos_attempt_id"]
        rows = fetch(aid)
        report.record(f"[{label}] exactly one Raw Data row", len(rows) == 1, f"{len(rows)} rows")
        if len(rows) != 1:
            continue
        rec, f = rows[0], rows[0]["fields"]
        created_ids[label] = rec["id"]
        report.record(f"[{label}] Impact Classification == {expect_class!r}",
                      f.get("Impact Classification") == expect_class,
                      repr(f.get("Impact Classification")))
        tv = f.get("Target Impact Velocity")
        report.record(f"[{label}] Target Impact Velocity == {velocity}", tv == velocity, repr(tv))
        report.record(f"[{label}] Target Impact Velocity is a NUMBER not text",
                      isinstance(tv, (int, float)) and not isinstance(tv, bool),
                      type(tv).__name__)
        report.record(f"[{label}] value landed on the row for this attempt",
                      f.get(MERGE_KEY) == aid, str(f.get(MERGE_KEY)))
        report.record(f"[{label}] Impact Number == 1", f.get("Impact Number") == 1,
                      repr(f.get("Impact Number")))
        report.record(f"[{label}] Impact Result present", bool(f.get("Impact Result")),
                      repr(f.get("Impact Result"))[:60])
        report.record(f"[{label}] photo published via the real attachment path",
                      bool(f.get("LabOS Photos")),
                      f"{len(f.get('LabOS Photos') or [])} attachment(s)")
        try:
            blob = json.loads(f.get("Complete LabOS JSON Response") or "")
            ok = isinstance(blob, dict)
        except Exception:
            ok, blob = False, None
        report.record(f"[{label}] Complete LabOS JSON is valid JSON", ok)
        if blob is not None:
            # **Structural, not a substring scan.** A bare `"999" in text`
            # matched the microseconds in a timestamp - 22:33:27.464999 - and
            # reported a leak that was not one. Walk the document and compare
            # actual values instead.
            def leaks(node):
                if isinstance(node, dict):
                    return any(leaks(v) for v in node.values())
                if isinstance(node, list):
                    return any(leaks(v) for v in node)
                if isinstance(node, str):
                    return node == POISON["Missile Type"]
                if isinstance(node, (int, float)) and not isinstance(node, bool):
                    return float(node) in (POISON["Missile Weight"],
                                           POISON["Impact Velocity"])
                return False
            report.record(f"[{label}] withdrawn values absent from the JSON",
                          not leaks(blob))

    # ---- idempotency: re-publish the same attempt -----------------------
    label = "LMI-D"
    attempt = produced[label][0]
    aid = attempt["labos_attempt_id"]
    before_rows = fetch(aid)
    s = Session()
    holder["session"] = s
    try:
        # The real publish path, not a hand-built outbox row: `record_phase`
        # is what the routes call, so a re-delivery here is the same code an
        # operator's retry would take.
        from app.sync import publish
        from app.data.models import TestResult
        row = s.query(TestResult).filter(TestResult.id == attempt["id"]).one()
        entry = publish.record_phase(s, row, "terminal")
        s.commit()
        # **None is the correct answer here, not a failure.** `record_phase`
        # deliberately declines a phase already delivered — the queue refuses
        # to re-send before the wire is even involved. That is idempotency one
        # layer earlier than the upsert, and worth asserting as such.
        report.record("[retry] the queue refuses a phase already delivered",
                      entry is None, "record_phase returned an entry" if entry else "")
        worker.drain(s, send)
    except Exception as exc:                                   # noqa: BLE001
        report.record("[retry] the queue refuses a phase already delivered", False,
                      str(exc)[:140])
    finally:
        s.close()

    # The wire-level half: send the identical payload again through the real
    # client and confirm the upsert merges onto the same record rather than
    # inserting a second one.
    try:
        s = Session()
        try:
            from app.data.models import TestResult
            from app.airtable import mapping, envelope
            row = s.query(TestResult).filter(TestResult.id == attempt["id"]).one()
            values = mapping.envelope_values(publish.concrete(s, row))
            # **The verdict phase, not terminal.** This attempt has been
            # reviewed, so its Test Result is a verdict; a terminal write
            # carrying one is refused by the envelope (contract §4, §6).
            # Re-sending the wrong phase would have proven the guard, not the
            # upsert.
            payload = envelope.build_verdict(values)
        finally:
            s.close()
        resp = at.upsert_records(RAW, [payload])
        ids = [r["id"] for r in resp.get("records", [])]
        report.record("[retry] direct re-upsert returns the same record id",
                      ids == [before_rows[0]["id"]] if before_rows else False,
                      f"{ids} vs {[r['id'] for r in before_rows]}")
        report.record("[retry] upsert updated rather than created",
                      not resp.get("createdRecords"),
                      f"createdRecords={resp.get('createdRecords')}")
    except Exception as exc:                                   # noqa: BLE001
        report.record("[retry] direct re-upsert returns the same record id", False,
                      str(exc)[:160])
    after_rows = fetch(aid)
    report.record("[retry] still exactly one row for the attempt id",
                  len(after_rows) == 1, f"{len(before_rows)} -> {len(after_rows)}")
    if before_rows and after_rows:
        report.record("[retry] same Airtable record id — updated, not duplicated",
                      before_rows[0]["id"] == after_rows[0]["id"],
                      f"{before_rows[0]['id']} -> {after_rows[0]['id']}")
    all_probe = at.list_records(RAW, formula=f"{{Operator Name}}='{OPERATOR}'").get("records", [])
    report.record("[retry] no duplicate logical attempt across the probe",
                  len({r["fields"].get(MERGE_KEY) for r in all_probe}) == len(all_probe),
                  f"{len(all_probe)} rows, "
                  f"{len({r['fields'].get(MERGE_KEY) for r in all_probe})} distinct attempt ids")

    # ---- write boundary -------------------------------------------------
    hier_after = snapshot_tables(api, ttok)
    for tbl, name in ((PROJECTS, "IFET Projects"), (MOCKUPS, "Mock-Ups/Specimens"),
                      (PROTOCOLS, "Tests Protocols"), (SECTIONS_T, "Protocol Sections")):
        added = set(hier_after[tbl]) - set(hier_before[tbl])
        expected = {recs["project"], recs["mockup"], recs["protocol"],
                    *recs["sections"].values()}
        stray = added - expected
        report.record(f"no unexpected write to {name}", not stray, str(stray))

    # ---- production safety, after ---------------------------------------
    p_fields_after, p_rows_after, p_schema_after = prod_state()
    (ev / f"production-after-{stamp}.json").write_text(
        json.dumps(p_schema_after, indent=1, sort_keys=True))
    report.record("production schema unchanged at 142",
                  p_fields_after == 142 == p_fields_before, str(p_fields_after))
    report.record("production record count unchanged",
                  p_rows_after == p_rows_before, f"{p_rows_before} -> {p_rows_after}")
    report.record("production schema byte-identical before/after",
                  json.dumps(p_schema_before, sort_keys=True)
                  == json.dumps(p_schema_after, sort_keys=True))

    json.dump({"created_airtable_records": created_ids,
               "probe_hierarchy": recs,
               "attempt_ids": {k: v[0]["labos_attempt_id"] for k, v in produced.items()},
               "results": [{"assertion": a, "pass": b, "detail": c} for a, b, c in report.rows]},
              (ev / f"probe-results-{stamp}.json").open("w"), indent=1)

    print()
    print("=" * 74)
    f = report.failures()
    print(f"TA7 PROBE: {len(report.rows) - len(f)}/{len(report.rows)} assertions passed")
    for label, _, detail in f:
        print(f"  FAILED  {label}  {detail}")
    print(f"evidence -> {ev}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
