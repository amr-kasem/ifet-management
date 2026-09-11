"""Five-workflow end-to-end acceptance — the backend half, against Testing.

    python3 -m tests.e2e_acceptance --destroy-db m2 --evidence DIR
    python3 -m tests.e2e_acceptance --destroy-db m2 --evidence DIR --live \
        --approved-by "who, when" [--run-tag RUN-2026-09-11-A]

**This is an orchestrator, not a framework.** Every mechanism it uses already
exists and is already proven: `Report`, the disposable-database guard, the
stack builder, the hierarchy snapshot and the production-safety rules are
imported from `ta7_probe_live`, exactly as `ta6_probe_live` imports them. What
is here is the composition — all five workflows in one run, against one
hierarchy, with one inventory at the end.

**What it is for.** After the UI developer integrates their screens, the
acceptance run is a person driving those screens while this asserts what
reached the database and the Testing base. Run it before that and it is a
regression check on the backend half; run it after, and the human steps in
`docs/labos-airtable/testing/five-workflow-e2e-acceptance-2026-09-11.md`
replace the API calls below while these assertions stay exactly as they are.

**What it deliberately does NOT do.** The negative requirement cases — a
malformed section, a missing direction, a bad unit, an unknown code, an
unsupported static programme — are covered by `test_requirement_release.py`
and `test_inbound_import.py` at API level, where they run in seconds against
every commit. Reproducing them on the live wire would add a minute per case
and prove nothing further: none of them involves Airtable beyond the mirror,
which is local. The acceptance document cites those tests as AUTOMATED BACKEND
ASSERTIONS rather than duplicating them here. The **one** negative case that
does belong on the wire is the DG14 disagreement, because the value it
disagrees with is mirrored from a live Airtable row.

Safety, none of it a flag:

1. Production is refused unconditionally, read before and after, compared.
2. Dry run is the default.
3. `--approved-by` is mandatory with `--live`.
4. `--destroy-db` is mandatory and must match the database in the URL.
5. Everything created is tagged with the run tag. **LabOS never deletes.**
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

ENV_PATH = "/home/gad/AWS/ifet-project/ifet-management/.env"
FE, ANSI = "Forced Entry", "ANSI Z97.1"
FE_FIELD, ANSI_FIELD = "Forced Entry Result", "ANSI Result"
DP = (60.0, 45.0)

# Three impacts, one per classification, so SMI / LMI-D / LMI-E are all on the
# wire in one run rather than across three.
IMPACTS = [("SMI", "IMPACT_SMI", None, "SMI", 130.0),
           ("LMI-D", "IMPACT_LMI", "D", "LMI Level D", 50.25),
           ("LMI-E", "IMPACT_LMI", "E", "LMI Level E", 55.5)]


def seed_airtable(api, token, report, tag):
    """One hierarchy carrying all five requirement codes, tagged for this run."""
    def create(table, fields):
        r = api("POST", f"https://api.airtable.com/v0/{BASE_TESTING}/{table}",
                token, {"records": [{"fields": fields}], "typecast": True})
        return r["records"][0]["id"]

    job_no = f"IFET-E2E-{tag}"
    job = create(PROJECTS, {"IFET job number": job_no,
                            "Project name": f"E2E acceptance {tag}"})
    mock = create(MOCKUPS, {"Mock-up/specimen name": f"E2E specimen {tag}",
                            "IFET Job Number": [job]})
    proto = create(PROTOCOLS, {"Protocol Name": f"E2E protocol {tag}",
                               "Mock-Up": [mock]})
    secs = {}
    for code, extra in (
            ("STATIC_PRESSURE", {"Requirement Kind": "Directional Pair",
                                 "Required Value Inward": DP[0],
                                 "Required Value Outward": DP[1],
                                 "Required Unit": "PSF"}),
            ("CYCLIC_PRESSURE", {"Requirement Kind": "Directional Pair",
                                 "Required Value Inward": DP[0],
                                 "Required Value Outward": DP[1],
                                 "Required Unit": "PSF"}),
            ("IMPACT_SMI", {"Requirement Kind": "Count", "Required Value": 1,
                            "Required Unit": "impacts"}),
            ("IMPACT_LMI", {"Requirement Kind": "Count", "Required Value": 2,
                            "Required Unit": "impacts"}),
            # **`Not Applicable`, not `Enum`.** `requirements.KIND_BY_CODE`
            # pins both pass/fail codes to it: the grade travels in
            # `Required Option` and there is no numeric requirement at all.
            ("FORCED_ENTRY", {"Requirement Kind": "Not Applicable",
                              "Required Option": "ASTM F588 Grade 40"}),
            ("ANSI_IMPACT", {"Requirement Kind": "Not Applicable",
                             "Required Option": "Class A"})):
        secs[code] = create(SECTIONS_T, {
            "Section Name": f"{code} ({tag})", "Test Protocol": [proto],
            "Requirement Code": code, "Applicability": "Required", **extra})
    report.record("hierarchy created, all five requirement codes", True,
                  f"job={job_no} {job}")
    return {"project": job, "mockup": mock, "protocol": proto,
            "sections": secs, "job_number": job_no}


def main(argv=None):                                            # noqa: PLR0915
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--approved-by", metavar="WHO")
    ap.add_argument("--destroy-db", metavar="NAME", required=True)
    ap.add_argument("--evidence", metavar="DIR", required=True)
    ap.add_argument("--run-tag", metavar="TAG", default=None,
                    help="deterministic per-run tag; defaults to the UTC stamp")
    args = ap.parse_args(argv)

    url = os.environ.get("M2_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        print("ERROR: set M2_DATABASE_URL to the harness PostgreSQL URL.", file=sys.stderr)
        return 2
    assert_disposable(url, args.destroy_db)

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

    from app.airtable.apply_schema import api, load_env
    from app.airtable.client import AirtableClient
    from app.airtable import mirror
    from app.sync import service, worker

    env = load_env(ENV_PATH)
    ptok, ttok = env["AIRTABLE_TOKEN_PRODUCTION"], airtable_settings.token
    ev = pathlib.Path(args.evidence); ev.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = args.run_tag or stamp
    # **One tag, everywhere.** Operator name, job number and section names all
    # carry it, so a later cleanup can be scoped to one run rather than to
    # "everything that looks synthetic".
    operator = f"LABOS-E2E-{tag}"

    print("=" * 74)
    print(f"FIVE-WORKFLOW E2E ACCEPTANCE — run tag {tag}")
    print("=" * 74)
    print(f"  database : {url.split('@')[-1]}")
    print(f"  base     : {airtable_settings.base_id} (testing)")
    print(f"  mode     : {'LIVE — this will write' if args.live else 'DRY RUN'}")
    if args.live:
        print(f"  approved : {args.approved_by}")
    print()

    report = Report()

    def prod_state():
        t = api("GET", f"https://api.airtable.com/v0/meta/bases/{BASE_PRODUCTION}/tables", ptok)
        n = sum(len(x["fields"]) for x in t["tables"])
        recs = api("GET", f"https://api.airtable.com/v0/{BASE_PRODUCTION}/{RAW}?pageSize=100",
                   ptok).get("records", [])
        return n, len(recs), t
    p_fields_before, p_rows_before, p_before = prod_state()
    (ev / f"production-before-{stamp}.json").write_text(
        json.dumps(p_before, indent=1, sort_keys=True))
    report.record("production schema before = 142", p_fields_before == 142, str(p_fields_before))

    client, Session = build_stack(url)
    if not args.live:
        print("\n  DRY RUN — nothing was created or sent. Re-run with --live.")
        return 1 if report.failures() else 0

    hier_before = snapshot_tables(api, ttok)
    seed_local(Session)
    recs = seed_airtable(api, ttok, report, tag)

    at = AirtableClient(settings=airtable_settings)
    s = Session()
    try:
        mirror.refresh(s, at); s.commit()
    finally:
        s.close()

    r = client.post("/airtable/import", json={
        "device_id": 1, "project_record_id": recs["project"],
        "specimen_record_id": recs["mockup"], "protocol_record_id": recs["protocol"]})
    report.record("W0 import succeeded", r.status_code == 200, r.text[:200])
    if r.status_code != 200:
        return 1
    project = r.json(); pid = project["id"]
    json.dump({"project": project}, (ev / f"imported-project-{stamp}.json").open("w"), indent=1)
    manual = {t["type"]: t for t in project["manual_tests"]}
    report.record("W0 six static + eight cyclic derived from the pair",
                  len(project["static_tests"]) == 6 and len(project["cyclic_tests"]) == 8,
                  f"{len(project['static_tests'])}/{len(project['cyclic_tests'])}")
    report.record("W0 two impact tests and two manual tests created",
                  len(project["missile_impact_tests"]) == 2 and set(manual) == {FE, ANSI},
                  f"impact={len(project['missile_impact_tests'])} manual={sorted(manual)}")

    holder = {}
    send = service.make_sender(at, airtable_settings,
                               session_for=lambda e: holder.get("session"))

    def drain(times=4):
        for _ in range(times):
            s = Session(); holder["session"] = s
            try:
                worker.drain(s, send)
            finally:
                s.close()

    def rows_for(aid):
        return at.list_records(RAW, formula=f"{{{MERGE_KEY}}}='{aid}'").get("records", [])

    # ---- DG14: the gate, on the wire -------------------------------------
    blocked = client.put(f"/projects/{pid}/static_tests/0/start",
                         json={"operator_name": operator})
    report.record("DG14 unverified imported requirement cannot start a rig",
                  blocked.status_code == 409, f"{blocked.status_code}")
    wrong = client.post(f"/projects/{pid}/requirement-verification", json={
        "inward_psf": 9.0, "outward_psf": 9.0, "unit": "PSF",
        "reference": f"E2E {tag}", "verified_by": operator})
    report.record("DG14 a pair disagreeing with the mirrored Airtable row is refused",
                  wrong.status_code == 409, f"{wrong.status_code}")
    ver = client.post(f"/projects/{pid}/requirement-verification", json={
        "inward_psf": DP[0], "outward_psf": DP[1], "unit": "PSF",
        "reference": f"Proposal E2E {tag} rev A", "verified_by": operator})
    report.record("DG14 the agreeing pair releases the job",
                  ver.status_code == 200 and ver.json().get("executable") is True,
                  f"{ver.status_code}")

    created = {}

    # ---- W1 Static Load ---------------------------------------------------
    st = client.post(f"/projects/{pid}/static_tests/0/trials", json={
        "operator_name": operator, "result": True, "testing_continued": "Stopped",
        "deflections": [{"deflection_gauge": "g1", "max_deflection": 1234.0,
                         "permanent_deflection": 12.0, "recovery": 60.0}]})
    report.record("W1 Static Load trial accepted", st.status_code in (200, 201), st.text[:120])

    # ---- W2 Cycles --------------------------------------------------------
    # The rig reports progress first: `Cycles Completed` is snapshotted at
    # termination and §6 forbids substituting zero for unknown, so a stage
    # closed without a reported count has its terminal payload refused.
    client.put(f"/projects/{pid}/cyclic_tests/0/update_status", json={"current_cycle": 4500})
    cy = client.post(f"/projects/{pid}/cyclic-tests/0/trials", json={
        "operator_name": operator, "result": True, "testing_continued": "Stopped",
        "deflections": [{"deflection_gauge": "g1", "max_deflection": 1000.0,
                         "permanent_deflection": 8.0, "recovery": 60.0}]})
    report.record("W2 Cycles trial accepted", cy.status_code in (200, 201), cy.text[:120])

    # ---- W3 Impact — three classifications, two impacts on one test -------
    by_family = {t["impact_family"]: t for t in project["missile_impact_tests"]}
    impact_attempts = []
    for label, code, level, expect, velocity in IMPACTS:
        family = "SMI" if code == "IMPACT_SMI" else "LMI"
        test_id = by_family[family]["id"]
        if label == "LMI-E":
            # The level is frozen by a completed attempt, so a second LMI
            # classification needs its own test — which is the model, not a
            # workaround. Both ids: a section without a protocol is a
            # half-finished binding and mapping.py refuses to publish it.
            t2 = client.post(f"/projects/{pid}/impact-tests/", json={
                "airtable_section_id": recs["sections"]["IMPACT_LMI"],
                "airtable_protocol_id": recs["protocol"],
                "airtable_section_name": f"IMPACT_LMI ({tag})"})
            t2.raise_for_status(); test_id = t2.json()["id"]
        patch = {"target_velocity": velocity}
        if level:
            patch["impact_level"] = level
        pr = client.patch(f"/projects/{pid}/impact-tests/{test_id}", json=patch)
        report.record(f"W3 [{label}] classification derives to {expect!r}",
                      pr.status_code == 200
                      and pr.json().get("impact_classification") == expect,
                      str(pr.json().get("impact_classification")))
        a = client.post(f"/projects/{pid}/impact-tests/{test_id}/trials",
                        json={"operator_name": operator})
        a.raise_for_status(); attempt = a.json()
        sh = client.post(f"/test-results/{attempt['id']}/shots", json={"result": True})
        sh.raise_for_status()
        # Required, not decoration: a completed impact attempt with no
        # photograph is refused, and evidence cannot be added after review.
        client.post(f"/shots/{sh.json()['id']}/photos", files=_jpeg(f"{label}.jpg"))
        fin = client.put(f"/test-results/{attempt['id']}/finish",
                         json={"result": True, "testing_continued": "Stopped"})
        report.record(f"W3 [{label}] finish accepted", fin.status_code == 200, fin.text[:120])
        client.put(f"/test-results/{attempt['id']}/verdict",
                   json={"test_result": "Pass", "verdict_by": operator,
                         "retest_required": False}).raise_for_status()
        impact_attempts.append((label, attempt, expect, velocity))

    # ---- W4 / W5 the two manual types, terminal then verdict --------------
    manual_attempts = []
    for label, ttype, verdict, wire in (("W4 Forced Entry", FE, "Pass", "Passed"),
                                        ("W5 ANSI Z97.1", ANSI, "Fail", "Failed")):
        a = client.post(f"/projects/{pid}/manual-tests/{manual[ttype]['id']}/trials",
                        json={"operator_name": operator})
        a.raise_for_status(); attempt = a.json(); aid = attempt["labos_attempt_id"]
        client.post(f"/test-results/{attempt['id']}/photos", files=_jpeg(f"{label}.jpg"))
        client.put(f"/test-results/{attempt['id']}/finish",
                   json={"result": True, "testing_continued": "Stopped",
                         "note": f"E2E {tag}"}).raise_for_status()
        drain()
        rows = rows_for(aid)
        own, other = (FE_FIELD, ANSI_FIELD) if ttype == FE else (ANSI_FIELD, FE_FIELD)
        if len(rows) == 1:
            f = rows[0]["fields"]
            report.record(f"{label} terminal: Test Result and {own} both 'Pending'",
                          f.get("Test Result") == "Pending" and f.get(own) == "Pending",
                          f"{f.get('Test Result')!r}/{f.get(own)!r}")
            report.record(f"{label} terminal: {other} absent, not blank", other not in f)
        else:
            report.record(f"{label} exactly one row at terminal", False, f"{len(rows)} rows")
        client.put(f"/test-results/{attempt['id']}/verdict",
                   json={"test_result": verdict, "verdict_by": operator,
                         "retest_required": False}).raise_for_status()
        manual_attempts.append((label, attempt, wire, own, other))

    drain()

    # ---- what reached the base -------------------------------------------
    for label, attempt, wire, own, other in manual_attempts:
        aid = attempt["labos_attempt_id"]
        rows = rows_for(aid)
        report.record(f"{label} exactly one row after the verdict", len(rows) == 1,
                      f"{len(rows)} rows")
        if len(rows) != 1:
            continue
        f = rows[0]["fields"]; created[label] = rows[0]["id"]
        report.record(f"{label} Test Result == {own} == {wire!r}",
                      f.get("Test Result") == wire and f.get(own) == wire,
                      f"{f.get('Test Result')!r}/{f.get(own)!r}")
        report.record(f"{label} {other} still absent", other not in f)
        report.record(f"{label} photo published", bool(f.get("LabOS Photos")),
                      f"{len(f.get('LabOS Photos') or [])}")

    for label, attempt, expect, velocity in impact_attempts:
        aid = attempt["labos_attempt_id"]
        rows = rows_for(aid)
        report.record(f"W3 [{label}] exactly one row", len(rows) == 1, f"{len(rows)} rows")
        if len(rows) != 1:
            continue
        f = rows[0]["fields"]; created[f"W3 {label}"] = rows[0]["id"]
        report.record(f"W3 [{label}] Impact Classification == {expect!r}",
                      f.get("Impact Classification") == expect, repr(f.get("Impact Classification")))
        tv = f.get("Target Impact Velocity")
        report.record(f"W3 [{label}] Target Impact Velocity == {velocity} as a number",
                      tv == velocity and isinstance(tv, (int, float)) and not isinstance(tv, bool),
                      repr(tv))
        report.record(f"W3 [{label}] Impact Number present", f.get("Impact Number") is not None,
                      repr(f.get("Impact Number")))
        report.record(f"W3 [{label}] neither dedicated result field",
                      FE_FIELD not in f and ANSI_FIELD not in f)

    # The rig types do not return an attempt id, so find them the way a reader
    # of the base would — **scoped to this run's project record**, never to the
    # operator tag alone, or a run could pass on a previous run's rows.
    this_run = at.list_records(
        RAW, formula=f"{{Airtable Project ID}}='{recs['project']}'").get("records", [])
    by_type = {}
    for rec in this_run:
        by_type.setdefault(rec["fields"].get("Test Type"), []).append(rec)
    for label, ttype in (("W1 Static Load", "Static Load"), ("W2 Cycles", "Cycles")):
        rs = by_type.get(ttype, [])
        report.record(f"{label} reached the base", len(rs) == 1, f"{len(rs)} row(s)")
        for rec in rs:
            created[label] = rec["id"]
            report.record(f"{label} neither dedicated result field",
                          FE_FIELD not in rec["fields"] and ANSI_FIELD not in rec["fields"])
    report.record("W3 Impact produced one row per physical impact",
                  len(by_type.get("Impact", [])) == len(IMPACTS),
                  f"{len(by_type.get('Impact', []))} of {len(IMPACTS)}")
    report.record("all five test types present in this run",
                  set(by_type) == {"Static Load", "Cycles", "Impact", FE, ANSI},
                  str(sorted(by_type)))
    report.record("one row per attempt, no duplicates",
                  len({r["fields"].get(MERGE_KEY) for r in this_run}) == len(this_run),
                  f"{len(this_run)} rows")

    # ---- retry does not duplicate ----------------------------------------
    label, attempt, wire, own, _ = manual_attempts[0]
    aid = attempt["labos_attempt_id"]
    before = rows_for(aid)
    try:
        s = Session()
        try:
            from app.data.models import TestResult
            from app.airtable import envelope, mapping
            from app.sync import publish
            row = s.query(TestResult).filter(TestResult.id == attempt["id"]).one()
            payload = envelope.build_verdict(mapping.envelope_values(publish.concrete(s, row)))
        finally:
            s.close()
        resp = at.upsert_records(RAW, [payload])
        ids = [r["id"] for r in resp.get("records", [])]
        report.record("retry: re-upsert returns the same record id",
                      ids == [before[0]["id"]] if before else False, str(ids))
        report.record("retry: updated rather than created",
                      not resp.get("createdRecords"), str(resp.get("createdRecords")))
    except Exception as exc:                                    # noqa: BLE001
        report.record("retry: re-upsert returns the same record id", False, str(exc)[:160])
    after = rows_for(aid)
    report.record("retry: still exactly one row for the attempt id", len(after) == 1,
                  f"{len(before)} -> {len(after)}")

    # ---- write boundary and production ------------------------------------
    hier_after = snapshot_tables(api, ttok)
    expected = {recs["project"], recs["mockup"], recs["protocol"], *recs["sections"].values()}
    for tbl, name in ((PROJECTS, "IFET Projects"), (MOCKUPS, "Mock-Ups/Specimens"),
                      (PROTOCOLS, "Tests Protocols"), (SECTIONS_T, "Protocol Sections")):
        stray = (set(hier_after[tbl]) - set(hier_before[tbl])) - expected
        report.record(f"no unexpected write to {name}", not stray, str(stray))

    p_fields_after, p_rows_after, p_after = prod_state()
    (ev / f"production-after-{stamp}.json").write_text(
        json.dumps(p_after, indent=1, sort_keys=True))
    report.record("production schema unchanged at 142",
                  p_fields_after == 142 == p_fields_before, str(p_fields_after))
    report.record("production record count unchanged", p_rows_after == p_rows_before,
                  f"{p_rows_before} -> {p_rows_after}")
    report.record("production schema byte-identical before/after",
                  json.dumps(p_before, sort_keys=True) == json.dumps(p_after, sort_keys=True))

    json.dump({"run_tag": tag, "operator_name": operator,
               "created_airtable_records": created,
               "hierarchy": recs,
               "attempt_ids": {l: a["labos_attempt_id"] for l, a, *_ in manual_attempts}
                              | {f"W3 {l}": a["labos_attempt_id"] for l, a, *_ in impact_attempts},
               "results": [{"assertion": a, "pass": b, "detail": c} for a, b, c in report.rows]},
              (ev / f"acceptance-results-{stamp}.json").open("w"), indent=1)

    print()
    print("=" * 74)
    f = report.failures()
    print(f"E2E ACCEPTANCE [{tag}]: {len(report.rows) - len(f)}/{len(report.rows)} assertions passed")
    for lbl, _, detail in f:
        print(f"  FAILED  {lbl}  {detail}")
    print(f"evidence -> {ev}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
