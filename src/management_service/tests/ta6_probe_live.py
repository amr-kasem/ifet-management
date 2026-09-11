"""TA6 live probe — the per-standard result pair, end to end, against Testing.

    python3 -m tests.ta6_probe_live --destroy-db m2 --evidence DIR
    python3 -m tests.ta6_probe_live --destroy-db m2 --evidence DIR --live \
        --approved-by "who, when"

**What this proves that no local test can.** Every local suite injects a
transport that accepts any payload — right for testing the queue, and exactly
how a sender that would have rejected every photograph once passed 270 tests.
This drives the real chain:

    Airtable records -> mirror.refresh -> importer -> HTTP routes
      -> sync.publish -> sync_outbox -> worker.drain
      -> service.make_sender(AirtableClient) -> Airtable

It is deliberately the *same* framework as `ta7_probe_live`: safety rules,
`Report`, the disposable-database guard, the stack builder and the hierarchy
snapshot are imported from it rather than restated, so there is one probe
harness to trust and not two.

What is specific to TA6 is the matrix. `Forced Entry Result` and `ANSI Result`
carry the *same value* as `Test Result`, gated by test type, so the assertions
are about a projection and not about a new lifecycle:

    Forced Entry terminal  Test Result Pending  FE Pending   ANSI absent
    Forced Entry verdict   Test Result == FE    ANSI absent
    ANSI terminal          Test Result Pending  ANSI Pending FE absent
    ANSI verdict           Test Result == ANSI  FE absent
    Static / Cycles / Impact                    neither field, ever

All four option values are exercised on the wire — `Pending` at terminal,
`Passed`, `Failed` and `Inconclusive` at verdict — because the base holds the
wire spelling and `option_wire` is what translates. A select created with
`Pass`/`Fail` would pass every type check and reject every verdict LabOS sends.

**And the JSON-valve transition, which is why this probe exists at all.**
While the two columns did not exist, `contract` marked them `ABSENT` and the
envelope carried their values through `Complete LabOS JSON Response` as
`labos_extra.forced_entry_result`. That was correct — nothing was lost while
the schema caught up. Now that the columns exist the value must travel as a
*column*, and the valve must be empty of it. Both halves are asserted: the
pending-state envelope is rebuilt in-process to show what it used to do, and
the live record is checked to show it no longer does.

Safety, none of it a flag:

1. Production is refused unconditionally, and its schema and record counts are
   captured before and after and compared.
2. Dry run is the default; the local half runs and nothing is sent.
3. `--approved-by` is mandatory with `--live`.
4. Everything created is tagged `IFET-PROBE-TA6-0001` / `LABOS-PROBE-TA6`.
   **LabOS never deletes.** Records stay for review.
5. `--destroy-db` is mandatory and must name the database in the URL.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

from tests.ta7_probe_live import (BASE_PRODUCTION, BASE_TESTING, MERGE_KEY,
                                  MOCKUPS, PROJECTS, PROTOCOLS, RAW,
                                  SECTIONS_T, Report, _jpeg, assert_disposable,
                                  build_stack, seed_local, snapshot_tables)

JOB = "IFET-PROBE-TA6-0001"
OPERATOR = "LABOS-PROBE-TA6"
ENV_PATH = "/home/gad/AWS/ifet-project/ifet-management/.env"

FE, ANSI = "Forced Entry", "ANSI Z97.1"
FE_FIELD, ANSI_FIELD = "Forced Entry Result", "ANSI Result"

# **Every value the select can hold, on the real wire.** `Pending` arrives at
# terminal for free; the other three need one reviewed attempt each, and the
# fourth type pairing exists so `Inconclusive` is exercised too.
VERDICTS = [
    # label,      test type, verdict sent, expected wire value
    ("FE-pass",   FE,   "Pass",         "Passed"),
    ("FE-incon",  FE,   "Inconclusive", "Inconclusive"),
    ("ANSI-fail", ANSI, "Fail",         "Failed"),
]


def seed_airtable(api, token, report, run_id):
    """The probe's own hierarchy, carrying all five requirement codes.

    Five codes and not two: the point of TA6 is as much what the other three
    types *do not* emit as what these two do, and a claim about Static, Cycles
    and Impact is worth nothing unless those rows are on the wire beside them.
    """
    def create(table, fields):
        r = api("POST", f"https://api.airtable.com/v0/{BASE_TESTING}/{table}",
                token, {"records": [{"fields": fields}], "typecast": True})
        return r["records"][0]["id"]

    job_no = f"{JOB}-{run_id}"
    job = create(PROJECTS, {"IFET job number": job_no, "Project name": "TA6 probe"})
    mock = create(MOCKUPS, {"Mock-up/specimen name": "TA6 probe specimen",
                            "IFET Job Number": [job]})
    proto = create(PROTOCOLS, {"Protocol Name": "TA6 probe protocol",
                               "Mock-Up": [mock]})
    secs = {}
    # The design-pressure pair. The importer refuses a protocol without one -
    # the six static and eight cyclic stages derive from it - so this is not
    # optional scaffolding, it is what makes the other three codes importable.
    secs["STATIC_PRESSURE"] = create(SECTIONS_T, {
        "Section Name": "DP probe (PSF)", "Test Protocol": [proto],
        "Requirement Code": "STATIC_PRESSURE",
        "Requirement Kind": "Directional Pair", "Applicability": "Required",
        "Required Value Inward": 60, "Required Value Outward": 45,
        "Required Unit": "PSF"})
    # **A Directional Pair in PSF, not a cycle count.**
    # `requirements.KIND_BY_CODE` pins `CYCLIC_PRESSURE` to `Directional Pair`:
    # the section states the pressures the cycling runs between and LabOS
    # derives the eight stages and their counts from them. The first run of
    # this probe sent `Count`/`cycles`, the section was correctly refused, and
    # the consequence was visible and worth keeping in mind — the eight cyclic
    # stages are still derived from the design pressure, but nothing binds
    # them to an Airtable section, so each one lands in `GET /sync/failures`
    # as a non-recoverable "no Airtable linkage" rather than silently
    # publishing against the wrong requirement.
    secs["CYCLIC_PRESSURE"] = create(SECTIONS_T, {
        "Section Name": "Cycles probe (PSF)", "Test Protocol": [proto],
        "Requirement Code": "CYCLIC_PRESSURE",
        "Requirement Kind": "Directional Pair", "Applicability": "Required",
        "Required Value Inward": 60, "Required Value Outward": 45,
        "Required Unit": "PSF"})
    secs["IMPACT_SMI"] = create(SECTIONS_T, {
        "Section Name": "IMPACT_SMI (probe)", "Test Protocol": [proto],
        "Requirement Code": "IMPACT_SMI", "Requirement Kind": "Count",
        "Applicability": "Required", "Required Value": 1,
        "Required Unit": "impacts"})
    # **`Not Applicable`, not `Enum`.** `requirements.KIND_BY_CODE` pins both
    # of these to `Not Applicable`: a pass/fail test judged against a named
    # grade has no numeric requirement, and the grade travels in
    # `Required Option` rather than making the section an Enum. The first run
    # of this probe used `Enum` and both sections were correctly refused, so
    # neither manual test was created.
    secs["FORCED_ENTRY"] = create(SECTIONS_T, {
        "Section Name": "Forced Entry (probe)", "Test Protocol": [proto],
        "Requirement Code": "FORCED_ENTRY", "Requirement Kind": "Not Applicable",
        "Applicability": "Required", "Required Option": "ASTM F588 Grade 40"})
    secs["ANSI_IMPACT"] = create(SECTIONS_T, {
        "Section Name": "ANSI Z97.1 (probe)", "Test Protocol": [proto],
        "Requirement Code": "ANSI_IMPACT", "Requirement Kind": "Not Applicable",
        "Applicability": "Required", "Required Option": "Class A"})
    report.record("probe hierarchy created in Testing", True,
                  f"job={job_no} {job} protocol={proto} sections={list(secs.values())}")
    return {"project": job, "mockup": mock, "protocol": proto,
            "sections": secs, "job_number": job_no}


def _dedicated(fields):
    """The pair as the base actually returned them, absent included."""
    return fields.get(FE_FIELD), fields.get(ANSI_FIELD)


def main(argv=None):                                            # noqa: PLR0915
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

    # Load the .env before `app.config` is imported: `airtable_settings` is
    # built from os.getenv at import time, so a probe started without the
    # environment exported gets an empty token and a 401 that reads like a
    # permissions problem rather than a missing variable.
    from app.airtable.apply_schema import load_env as _load_env
    for k, v in _load_env(ENV_PATH).items():
        os.environ.setdefault(k, v)

    from app.config import airtable_settings
    if not airtable_settings.token:
        raise SystemExit("REFUSED: no Airtable token in the environment")
    if airtable_settings.base_id == BASE_PRODUCTION:
        raise SystemExit("REFUSED: configured base is production")
    if args.live and not args.approved_by:
        raise SystemExit('REFUSED: --live needs --approved-by "<who, and when>"')

    from app.airtable import contract as C
    from app.airtable import envelope, mapping, mirror
    from app.airtable.apply_schema import api, load_env
    from app.airtable.client import AirtableClient
    from app.sync import publish, service, worker

    env = load_env(ENV_PATH)
    ptok = env["AIRTABLE_TOKEN_PRODUCTION"]
    ttok = airtable_settings.token
    ev = pathlib.Path(args.evidence); ev.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print("=" * 74)
    print("TA6 LIVE PROBE — the per-standard result pair")
    print("=" * 74)
    print(f"  database : {url.split('@')[-1]}")
    print(f"  base     : {airtable_settings.base_id} (testing)")
    print(f"  mode     : {'LIVE — this will write' if args.live else 'DRY RUN'}")
    if args.live:
        print(f"  approved : {args.approved_by}")
    print()

    report = Report()

    # ---- the contract itself, before anything is sent --------------------
    #
    # If these are wrong the wire assertions below are meaningless: a field
    # still marked pending routes through the JSON valve and the column stays
    # empty, which is a passing-looking absence.
    for name, fid in ((FE_FIELD, "fldAHuPzZHZEj0Cjt"), (ANSI_FIELD, "fldmCKJV95N9uL7xt")):
        f = C.BY_LABOS_NAME[name]
        report.record(f"contract: {name} is PRESENT, not pending", f.expected_live and not f.pending_schema,
                      f"v2={f.v2} pending={f.pending_schema}")
        report.record(f"contract: {name} translates Pass->Passed, Fail->Failed",
                      f.wire_option("Pass") == "Passed" and f.wire_option("Fail") == "Failed",
                      str(f.option_wire))
        report.record(f"contract: {name} field id recorded as {fid}", fid in f.note, f.note[:60])
    report.record("contract: nothing is left pending_schema",
                  not [f.labos_name for f in C.FIELDS if f.pending_schema])

    # ---- production safety, before ---------------------------------------
    def prod_state():
        t = api("GET", f"https://api.airtable.com/v0/meta/bases/{BASE_PRODUCTION}/tables", ptok)
        n = sum(len(x["fields"]) for x in t["tables"])
        recs = api("GET", f"https://api.airtable.com/v0/{BASE_PRODUCTION}/{RAW}?pageSize=100",
                   ptok).get("records", [])
        return n, len(recs), t
    p_fields_before, p_rows_before, p_schema_before = prod_state()
    (ev / f"production-before-{stamp}.json").write_text(
        json.dumps(p_schema_before, indent=1, sort_keys=True))
    report.record("production schema before = 142", p_fields_before == 142, str(p_fields_before))
    prod_names = {f["name"] for t in p_schema_before["tables"] if t["id"] == RAW
                  for f in t["fields"]}
    for n in (FE_FIELD, ANSI_FIELD):
        report.record(f"production does not have {n}", n not in prod_names)

    # ---- the Testing schema, read independently of the tool that wrote it -
    tmeta = api("GET", f"https://api.airtable.com/v0/meta/bases/{BASE_TESTING}/tables", ttok)
    (ev / f"testing-schema-{stamp}.json").write_text(json.dumps(tmeta, indent=1, sort_keys=True))
    report.record("testing schema = 164", sum(len(t["fields"]) for t in tmeta["tables"]) == 164,
                  str(sum(len(t["fields"]) for t in tmeta["tables"])))
    raw_fields = {f["name"]: f for t in tmeta["tables"] if t["id"] == RAW for f in t["fields"]}
    for n in (FE_FIELD, ANSI_FIELD):
        f = raw_fields.get(n)
        report.record(f"{n} exists on the Raw Data table", bool(f), "" if f else "missing")
        if f:
            report.record(f"{n} is singleSelect", f["type"] == "singleSelect", f["type"])
            report.record(f"{n} choices are exactly the wire vocabulary",
                          [c["name"] for c in f["options"]["choices"]]
                          == ["Pending", "Passed", "Failed", "Inconclusive"],
                          str([c["name"] for c in f["options"]["choices"]]))

    client, Session = build_stack(url)

    if not args.live:
        print("\n  DRY RUN — nothing was created or sent. Re-run with --live.")
        return 1 if report.failures() else 0

    hier_before = snapshot_tables(api, ttok)
    seed_local(Session)
    recs = seed_airtable(api, ttok, report, stamp[-7:-1])

    at = AirtableClient(settings=airtable_settings)
    s = Session()
    try:
        mirror.refresh(s, at)
        s.commit()
    finally:
        s.close()

    r = client.post("/airtable/import", json={
        "device_id": 1, "project_record_id": recs["project"],
        "specimen_record_id": recs["mockup"], "protocol_record_id": recs["protocol"]})
    report.record("import succeeded", r.status_code == 200, r.text[:200])
    if r.status_code != 200:
        return 1
    project = r.json()
    pid = project["id"]
    json.dump({"project": project}, (ev / f"imported-project-{stamp}.json").open("w"), indent=1)

    manual = {t["type"]: t for t in project["manual_tests"]}
    report.record("both manual tests created from their requirement codes",
                  set(manual) == {FE, ANSI}, str(sorted(manual)))
    report.record("six static stages derived from the design pressure",
                  len(project["static_tests"]) == 6, str(len(project["static_tests"])))
    report.record("eight cyclic stages derived from the design pressure",
                  len(project["cyclic_tests"]) == 8, str(len(project["cyclic_tests"])))
    report.record("one impact test created", len(project["missile_impact_tests"]) == 1,
                  str(len(project["missile_impact_tests"])))
    if set(manual) != {FE, ANSI}:
        return 1

    def fetch(aid):
        return at.list_records(RAW, formula=f"{{{MERGE_KEY}}}='{aid}'").get("records", [])

    holder = {}
    send = service.make_sender(at, airtable_settings,
                               session_for=lambda entry: holder.get("session"))

    def drain(times=4):
        for _ in range(times):
            s = Session()
            holder["session"] = s
            try:
                worker.drain(s, send)
            finally:
                s.close()

    produced = {}

    # ---- the two manual types, terminal then verdict ---------------------
    for label, ttype, verdict, wire in VERDICTS:
        test_id = manual[ttype]["id"]
        a = client.post(f"/projects/{pid}/manual-tests/{test_id}/trials",
                        json={"operator_name": OPERATOR})
        a.raise_for_status()
        attempt = a.json()
        aid = attempt["labos_attempt_id"]
        client.post(f"/test-results/{attempt['id']}/photos", files=_jpeg(f"{label}.jpg"))
        fin = client.put(f"/test-results/{attempt['id']}/finish",
                         json={"result": True, "testing_continued": "Stopped"})
        report.record(f"[{label}] finish accepted", fin.status_code == 200, fin.text[:120])
        drain()

        # -- terminal: Pending in both the shared and the dedicated column --
        rows = fetch(aid)
        report.record(f"[{label}] exactly one Raw Data row at terminal", len(rows) == 1,
                      f"{len(rows)} rows")
        if len(rows) != 1:
            continue
        f = rows[0]["fields"]
        own, other = (FE_FIELD, ANSI_FIELD) if ttype == FE else (ANSI_FIELD, FE_FIELD)
        report.record(f"[{label}] terminal Test Result == 'Pending'",
                      f.get("Test Result") == "Pending", repr(f.get("Test Result")))
        report.record(f"[{label}] terminal {own} == 'Pending'", f.get(own) == "Pending",
                      repr(f.get(own)))
        report.record(f"[{label}] terminal {other} is absent, not blank",
                      other not in f, repr(f.get(other)))
        report.record(f"[{label}] Test Type is {ttype!r}", f.get("Test Type") == ttype,
                      repr(f.get("Test Type")))

        # -- the JSON valve is empty of it now that the column exists -------
        blob = json.loads(f.get("Complete LabOS JSON Response") or "{}")
        extra = blob.get("labos_extra") or {}
        report.record(f"[{label}] the value is NOT in the JSON valve any more",
                      not any(k in extra for k in ("forced_entry_result", "ansi_result")),
                      str(sorted(extra)))

        # -- verdict: the same value in both columns ------------------------
        client.put(f"/test-results/{attempt['id']}/verdict",
                   json={"test_result": verdict, "verdict_by": OPERATOR,
                         "retest_required": False}).raise_for_status()
        drain()
        rows = fetch(aid)
        report.record(f"[{label}] still exactly one row after the verdict", len(rows) == 1,
                      f"{len(rows)} rows")
        if len(rows) != 1:
            continue
        f = rows[0]["fields"]
        report.record(f"[{label}] verdict Test Result == {wire!r}",
                      f.get("Test Result") == wire, repr(f.get("Test Result")))
        report.record(f"[{label}] verdict {own} == {wire!r}", f.get(own) == wire, repr(f.get(own)))
        report.record(f"[{label}] Test Result == {own} — one value, two columns",
                      f.get("Test Result") == f.get(own),
                      f"{f.get('Test Result')!r} vs {f.get(own)!r}")
        report.record(f"[{label}] {other} still absent after the verdict", other not in f,
                      repr(f.get(other)))
        report.record(f"[{label}] photo published via the real attachment path",
                      bool(f.get("LabOS Photos")),
                      f"{len(f.get('LabOS Photos') or [])} attachment(s)")
        try:
            ok = isinstance(json.loads(f.get("Complete LabOS JSON Response") or ""), dict)
        except Exception:                                       # noqa: BLE001
            ok = False
        report.record(f"[{label}] Complete LabOS JSON is valid JSON", ok)
        produced[label] = (attempt, rows[0]["id"], wire, own)

    # ---- the other three types emit neither field ------------------------
    other_types = {}

    st = client.post(f"/projects/{pid}/static_tests/0/trials", json={
        "operator_name": OPERATOR, "result": True, "testing_continued": "Stopped",
        "deflections": [{"deflection_gauge": "g1", "max_deflection": 1234.0,
                         "permanent_deflection": 12.0, "recovery": 60.0}]})
    report.record("[Static Load] trial accepted", st.status_code in (200, 201), st.text[:120])

    # **The rig reports progress first, and it has to.** `Cycles Completed` is
    # snapshotted from `cyclic_tests.current_cycle` at termination, and §6
    # forbids substituting zero for unknown — so a stage closed without a
    # reported count has its terminal payload refused, visibly, rather than
    # publishing a cycle count nobody measured. The first run of this probe
    # skipped `update_status` and saw exactly that refusal.
    us = client.put(f"/projects/{pid}/cyclic_tests/0/update_status",
                    json={"current_cycle": 4500})
    report.record("[Cycles] rig-reported progress accepted", us.status_code == 200,
                  us.text[:120])
    cy = client.post(f"/projects/{pid}/cyclic-tests/0/trials", json={
        "operator_name": OPERATOR, "result": True, "testing_continued": "Stopped",
        "deflections": [{"deflection_gauge": "g1", "max_deflection": 1000.0,
                         "permanent_deflection": 8.0, "recovery": 60.0}]})
    report.record("[Cycles] trial accepted", cy.status_code in (200, 201), cy.text[:120])

    imp_test = project["missile_impact_tests"][0]["id"]
    client.patch(f"/projects/{pid}/impact-tests/{imp_test}",
                 json={"target_velocity": 130.0}).raise_for_status()
    ia = client.post(f"/projects/{pid}/impact-tests/{imp_test}/trials",
                     json={"operator_name": OPERATOR})
    ia.raise_for_status()
    imp_attempt = ia.json()
    sh = client.post(f"/test-results/{imp_attempt['id']}/shots", json={"result": True})
    sh.raise_for_status()
    # **Required, not decoration.** TA7a's completion gate refuses a completed
    # impact attempt with no photograph — "evidence cannot be added after
    # review" — so an Impact row only reaches the base with one. The first run
    # of this probe omitted it and was correctly refused with a 400.
    client.post(f"/shots/{sh.json()['id']}/photos", files=_jpeg("impact.jpg"))
    fin = client.put(f"/test-results/{imp_attempt['id']}/finish",
                     json={"result": True, "testing_continued": "Stopped"})
    report.record("[Impact] finish accepted", fin.status_code == 200, fin.text[:120])
    client.put(f"/test-results/{imp_attempt['id']}/verdict",
               json={"test_result": "Pass", "verdict_by": OPERATOR,
                     "retest_required": False}).raise_for_status()
    drain()

    # The rig types do not return an attempt id from their trial routes, so
    # find their rows the way a reader of the base would. **Scoped to this
    # run's project record**, not to the operator tag: the tag is stable across
    # runs and earlier runs' rows are still in the base, so a tag-wide query
    # would let a run pass on a previous run's evidence.
    this_run = at.list_records(
        RAW, formula=f"{{Airtable Project ID}}='{recs['project']}'").get("records", [])
    report.record("this run's rows are distinguishable from earlier runs'",
                  bool(this_run), f"{len(this_run)} row(s) for {recs['project']}")
    by_type = {}
    for rec in this_run:
        by_type.setdefault(rec["fields"].get("Test Type"), []).append(rec)
    for ttype in ("Static Load", "Cycles", "Impact"):
        rows = by_type.get(ttype, [])
        report.record(f"[{ttype}] reached the base", bool(rows), f"{len(rows)} row(s)")
        for rec in rows:
            fe, ansi = _dedicated(rec["fields"])
            report.record(f"[{ttype}] emits neither dedicated field",
                          FE_FIELD not in rec["fields"] and ANSI_FIELD not in rec["fields"],
                          f"{FE_FIELD}={fe!r} {ANSI_FIELD}={ansi!r}")
        other_types[ttype] = [r["id"] for r in rows]

    # ---- the JSON-valve transition, both halves --------------------------
    #
    # The live rows above show the value travelling as a column. This shows
    # what the same attempt would have done while the column did not exist,
    # by rebuilding the envelope against a pending-state contract in process.
    # Nothing is sent: it is the *builder* that is under test, and the point
    # is that the two states differ in exactly the expected way.
    if "FE-pass" in produced:
        attempt = produced["FE-pass"][0]
        s = Session()
        try:
            from app.data.models import TestResult
            row = s.query(TestResult).filter(TestResult.id == attempt["id"]).one()
            values = mapping.envelope_values(publish.concrete(s, row))
        finally:
            s.close()
        report.record("[valve] mapping still produces the dedicated key",
                      values.get(FE_FIELD) == "Pass", repr(values.get(FE_FIELD)))
        live = envelope.build_verdict(values)
        report.record("[valve] PRESENT -> the value is a column",
                      live.get(FE_FIELD) == "Passed", repr(live.get(FE_FIELD)))
        live_extra = json.loads(live.get("Complete LabOS JSON Response") or "{}").get("labos_extra") or {}
        report.record("[valve] PRESENT -> and is NOT in labos_extra",
                      "forced_entry_result" not in live_extra, str(sorted(live_extra)))

        field = C.BY_LABOS_NAME[FE_FIELD]
        was_v2, was_pending = field.v2, field.pending_schema
        try:
            field.v2, field.pending_schema = C.ABSENT, True
            pending = envelope.build_verdict(values)
        finally:
            field.v2, field.pending_schema = was_v2, was_pending
        report.record("[valve] ABSENT -> the column is not sent",
                      FE_FIELD not in pending, repr(pending.get(FE_FIELD)))
        pending_extra = json.loads(
            pending.get("Complete LabOS JSON Response") or "{}").get("labos_extra") or {}
        # **The wire spelling, not the LabOS one.** The envelope translates
        # through `option_wire` before deciding where the value goes, so what
        # the valve preserved is exactly what the column would have held. That
        # is what makes the transition lossless rather than merely non-fatal.
        report.record("[valve] ABSENT -> the value is preserved in labos_extra",
                      pending_extra.get("forced_entry_result") == "Passed",
                      repr(pending_extra.get("forced_entry_result")))
        report.record("[valve] the contract is back to PRESENT after the check",
                      C.BY_LABOS_NAME[FE_FIELD].expected_live
                      and not C.BY_LABOS_NAME[FE_FIELD].pending_schema)

    # ---- idempotency: re-publish and re-upsert ---------------------------
    if "FE-pass" in produced:
        attempt, rec_id, wire, own = produced["FE-pass"]
        aid = attempt["labos_attempt_id"]
        before_rows = fetch(aid)
        s = Session()
        holder["session"] = s
        try:
            from app.data.models import TestResult
            row = s.query(TestResult).filter(TestResult.id == attempt["id"]).one()
            # **The watermark, read rather than provoked.** Re-recording an
            # already-delivered `terminal` would be refused — correctly, and
            # visibly, which is the point — but it would also leave a
            # delivered attempt marked `Sync Failed` and a false alarm in
            # `GET /sync/failures`. Nothing in the routes does that, so the
            # probe does not either: the guarantee is that anything behind the
            # delivered sequence is superseded, and that is a property of the
            # state, not something worth manufacturing a failure to observe.
            from app.sync.outbox import SyncAttemptState, is_superseded
            state = s.get(SyncAttemptState, row.labos_attempt_id)
            report.record("[retry] the verdict is the delivered watermark",
                          state is not None and (state.delivered_seq or 0) > 0,
                          f"delivered_seq={getattr(state, 'delivered_seq', None)}")

            class _Stale:                       # an entry from before the verdict
                phase, attempt_id = "terminal", row.labos_attempt_id
                attempt_seq = 1
                payload_updated_at = None
            report.record("[retry] a phase behind the watermark is superseded",
                          is_superseded(s, _Stale()))

            requeued = publish.record_phase(s, row, "verdict")
            s.commit()
            report.record("[retry] re-queuing the current phase is allowed",
                          requeued is not None,
                          "record_phase declined the current phase" if not requeued else "")
            worker.drain(s, send)
        except Exception as exc:                                # noqa: BLE001
            report.record("[retry] a phase behind the watermark is superseded", False,
                          str(exc)[:140])
        finally:
            s.close()

        try:
            s = Session()
            try:
                from app.data.models import TestResult
                row = s.query(TestResult).filter(TestResult.id == attempt["id"]).one()
                values = mapping.envelope_values(publish.concrete(s, row))
                payload = envelope.build_verdict(values)
            finally:
                s.close()
            resp = at.upsert_records(RAW, [payload])
            ids = [r["id"] for r in resp.get("records", [])]
            report.record("[retry] direct re-upsert returns the same record id",
                          ids == [rec_id], f"{ids} vs [{rec_id}]")
            report.record("[retry] upsert updated rather than created",
                          not resp.get("createdRecords"),
                          f"createdRecords={resp.get('createdRecords')}")
        except Exception as exc:                                # noqa: BLE001
            report.record("[retry] direct re-upsert returns the same record id", False,
                          str(exc)[:160])

        after_rows = fetch(aid)
        report.record("[retry] still exactly one row for the attempt id",
                      len(after_rows) == 1, f"{len(before_rows)} -> {len(after_rows)}")
        if after_rows:
            f = after_rows[0]["fields"]
            report.record("[retry] the dedicated value survived the re-send",
                          f.get(own) == wire, repr(f.get(own)))

    all_probe = at.list_records(RAW, formula=f"{{Operator Name}}='{OPERATOR}'").get("records", [])
    report.record("no duplicate logical attempt across the probe",
                  len({r["fields"].get(MERGE_KEY) for r in all_probe}) == len(all_probe),
                  f"{len(all_probe)} rows, "
                  f"{len({r['fields'].get(MERGE_KEY) for r in all_probe})} distinct attempt ids")

    # ---- write boundary --------------------------------------------------
    hier_after = snapshot_tables(api, ttok)
    for tbl, name in ((PROJECTS, "IFET Projects"), (MOCKUPS, "Mock-Ups/Specimens"),
                      (PROTOCOLS, "Tests Protocols"), (SECTIONS_T, "Protocol Sections")):
        added = set(hier_after[tbl]) - set(hier_before[tbl])
        expected = {recs["project"], recs["mockup"], recs["protocol"],
                    *recs["sections"].values()}
        stray = added - expected
        report.record(f"no unexpected write to {name}", not stray, str(stray))

    # ---- production safety, after ----------------------------------------
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

    json.dump({"created_airtable_records":
                   {k: v[1] for k, v in produced.items()} | other_types,
               "probe_hierarchy": recs,
               "attempt_ids": {k: v[0]["labos_attempt_id"] for k, v in produced.items()},
               "labos_test_ids": {k: v[0].get("labos_test_id") for k, v in produced.items()},
               "results": [{"assertion": a, "pass": b, "detail": c} for a, b, c in report.rows]},
              (ev / f"probe-results-{stamp}.json").open("w"), indent=1)

    print()
    print("=" * 74)
    f = report.failures()
    print(f"TA6 PROBE: {len(report.rows) - len(f)}/{len(report.rows)} assertions passed")
    for label, _, detail in f:
        print(f"  FAILED  {label}  {detail}")
    print(f"evidence -> {ev}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
