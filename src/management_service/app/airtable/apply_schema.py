"""Apply the decided LabOS field additions to the Airtable **Testing Base**.

Deliverable 2 of the 2026-09-06 IFET request: "make the required changes in the
Testing Base only, based on the LabOS requirements".

Safety rules, in order of importance:

1. **The production base is refused unconditionally.** There is no flag, env var
   or argument that lets this script write to `app0OCunbmuXl7Hc9`. Production
   schema delivery is a separately coordinated release step (plan M5).
2. **Dry run is the default.** `--apply` is required to issue any write.
3. **Only DECIDED fields are created.** The register also carries rows marked
   `OPEN`/`PROPOSED`, raised by IFET's workflow message and not yet agreed with
   them. An unwanted field is harder to remove than to add, so this script will
   not create one even if asked.
4. **Idempotent.** A field that already exists is reported and skipped, never
   duplicated or modified. Re-running after a partial failure is safe.
5. **Before and after schema are captured** to the output directory, so the
   change document is written from evidence rather than from intent.

Every field carries a `description` explaining why it exists. That is deliberate:
it makes the change self-documenting inside Airtable for whoever opens the base
next, not only in our repo.

    python -m app.airtable.apply_schema --out-dir ./evidence          # dry run
    python -m app.airtable.apply_schema --out-dir ./evidence --apply  # writes
"""

import argparse
import copy
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.request

TESTING_BASE = "app4oXS3Kd5IKWgJ7"
PRODUCTION_BASE = "app0OCunbmuXl7Hc9"

PROTOCOL_SECTIONS = "tblqpvuJlSdkeS9PS"
RAW_RESULTS = "tblnc9SsbXU0C0FWh"

ISO_DATETIME = {
    "dateFormat": {"name": "iso"},
    "timeFormat": {"name": "24hour"},
    "timeZone": "utc",
}


def _select(*names):
    return {"choices": [{"name": n} for n in names]}


# The 14 decided additions. Order matters only for readability of the log.
FIELDS = [
    # --- Protocol Sections: make a requirement row self-describing (contract 3.1)
    (PROTOCOL_SECTIONS, {
        "name": "Requirement Code",
        "type": "singleSelect",
        "options": _select(
            "STATIC_PRESSURE", "CYCLIC_PRESSURE", "IMPACT_LMI", "IMPACT_SMI",
            "FORCED_ENTRY", "ANSI_IMPACT", "GAUGE_COUNT", "STATIC_PROGRAMME",
            "WATER_PRESSURE"),
        "description": (
            "Stable code that tells LabOS which procedure a section means. LabOS routes "
            "work by this code, never by Section Name, so renaming a section can never "
            "silently change what gets executed. An unknown code stays visible but "
            "cannot start a test. WATER_PRESSURE is visible but unsupported in this "
            "release."),
    }),
    (PROTOCOL_SECTIONS, {
        "name": "Requirement Kind",
        "type": "singleSelect",
        "options": _select("Magnitude", "Directional Pair", "Count", "Enum",
                           "Not Applicable"),
        "description": (
            "The shape of the requirement, so LabOS knows how to read it without parsing "
            "the free-text Value. 'Not Applicable' means there is no numeric requirement "
            "(Forced Entry, ANSI) - it does NOT mean the test is unneeded; Applicability "
            "decides that."),
    }),
    (PROTOCOL_SECTIONS, {
        "name": "Applicability",
        "type": "singleSelect",
        "options": _select("Required", "Not Required", "Unconfirmed"),
        "description": (
            "Whether this section is actually required for this specimen. Only 'Required' "
            "is executable. Blank is treated as Unconfirmed, never as Not Required - LabOS "
            "will not infer that a test is unneeded from an empty cell, a blank Value or a "
            "historical Result."),
    }),
    (PROTOCOL_SECTIONS, {
        "name": "Required Value",
        "type": "number",
        "options": {"precision": 2},
        "description": (
            "The numeric requirement for Magnitude and Count kinds. Blank is not zero: a "
            "blank means unknown and blocks execution, while a real 0 is data. Leave Value "
            "untouched - it stays as the human-readable original."),
    }),
    (PROTOCOL_SECTIONS, {
        "name": "Required Value Inward",
        "type": "number",
        "options": {"precision": 2},
        "description": (
            "Inward design pressure as an independent positive magnitude in PSF. Required "
            "for pressure execution. Never derived by parsing a legacy string such as "
            "'+110/110', and never copied from the outward value - 46% of jobs are "
            "asymmetric."),
    }),
    (PROTOCOL_SECTIONS, {
        "name": "Required Value Outward",
        "type": "number",
        "options": {"precision": 2},
        "description": (
            "Outward design pressure as an independent positive magnitude in PSF. Held "
            "separately from inward on purpose: the two are independent numbers, not a "
            "sign convention."),
    }),
    (PROTOCOL_SECTIONS, {
        "name": "Required Unit",
        "type": "singleSelect",
        "options": _select("PSF", "in", "s", "cycles", "impacts"),
        "description": (
            "Unit for Required Value. Blank for a unitless count, an Enum or Not "
            "Applicable. Short forms only - LabOS rejects 'Inches' as an outbound token, "
            "so 'in' is canonical here too. Unit and Requirement Kind must agree."),
    }),
    (PROTOCOL_SECTIONS, {
        "name": "Required Option",
        "type": "singleLineText",
        "description": (
            "The explicit enum value for Enum-kind requirements, so LabOS never has to "
            "parse it out of Value. 'Full' is the initial supported static programme; an "
            "unrecognised option stays visible but cannot start a test."),
    }),

    # --- Protocol Sections: Impact requirements (added 2026-09-08) -----------
    #
    # A9 closed these as OMITTED on 2026-09-06, when LabOS read no requirement
    # values at all. That was reversed on 2026-09-08: LabOS reads the **typed**
    # fields and never parses the legacy `Value` string, which is what the PDF
    # extractor corrupts. Pre-fill is therefore safe by construction, and Impact
    # is the one test type it could not yet serve — the count arrives via
    # `Required Value` + `IMPACT_LMI`/`IMPACT_SMI`, but the missile did not.
    #
    # Three, not four. `Impact Locations` stays out: location is a per-shot
    # observation LabOS already records on `shots.area`, not a requirement, and
    # a speculative field now propagates into production rather than sitting
    # harmlessly in a sandbox.
    #
    # All three map 1:1 onto columns that already exist in the LabOS database —
    # `missile_impact_tests.missile`, `.missile_weight`, `shots.velocity` — so
    # nothing new has to be modelled to consume them.
    # --- WITHDRAWN 2026-09-10: Missile Type, Missile Weight, Impact Velocity
    #
    # Applied to the Testing Base on 2026-09-08 as inbound impact
    # requirements; withdrawn from the read contract two days later when the
    # product owner made the impact classification LabOS-owned. Their
    # definitions are removed from this list so the script **never proposes
    # them to any base again** — most importantly never to production, which
    # has never had them and stays at 142.
    #
    # **They are deliberately NOT deleted from the Testing Base.** This script
    # only ever creates: it prints SKIP for a field that already exists and has
    # no PATCH or DELETE path at all. Removing them from the shared base is a
    # later coordinated cleanup with the Airtable team, not a side effect of a
    # code change. `preflight.DEPRECATED_TESTING_ONLY` is what keeps asserting
    # that they remain present in Testing and absent from production.

    # --- Impact Number: the axis that keeps their roll-ups honest -----------
    #
    # Added 2026-09-08, after the product owner respecified Impact as **one
    # attempt per impact** and confirmed the consequence: a five-impact test
    # publishes five rows where it used to publish one.
    #
    # Without this field that change would ship the exact failure the change
    # document's §0.3 uses to justify `Corrects Attempt ID` — a roll-up counting
    # attempt rows would read five tests, and read plausibly. `Attempt Number`
    # already carries the ordinal, so this is not for LabOS's benefit: it is so a
    # consumer can count *tests* by `LabOS Test ID` and *impacts* by this,
    # without knowing a rule about which test type it is looking at. Structural
    # rather than conventional, for the same reason `Corrects Attempt ID` is.
    #
    # Blank on the other four test types. Delivery plan §4.5a.
    (RAW_RESULTS, {
        "name": "Impact Number",
        "type": "number",
        "options": {"precision": 0},
        "description": (
            "Which impact of the test this row records — 1, 2, 3. Populated only for "
            "Test Type = Impact, where one attempt is one impact, and blank on every "
            "other type. Count tests by grouping on LabOS Test ID; count impacts with "
            "this. A roll-up that counts attempt rows instead will read a five-impact "
            "test as five tests."),
    }),

    # --- the impact classification, 2026-09-10 ------------------------------
    #
    # The direction reversal. Airtable used to be asked for the missile, its
    # weight and a target velocity; the product owner replaced all three with
    # one classification chosen in LabOS and published back. Airtable's
    # requirement code already says IMPACT_SMI or IMPACT_LMI — the only fact it
    # never carried is Level D versus E, and that is the operator's.
    #
    # A single select rather than free text, so their views group and filter on
    # it natively. The cost is accepted deliberately: adding a future level
    # needs a coordinated schema change on their side, not just ours.
    #
    # **Derived, never stored and never an input.** LabOS holds
    # `impact_family` + `impact_level`; this string is a property over the
    # pair, so it cannot disagree with the requirement code the family was
    # frozen from.
    (RAW_RESULTS, {
        "name": "Impact Classification",
        "type": "singleSelect",
        "options": {"choices": [{"name": "SMI"},
                                {"name": "LMI Level D"},
                                {"name": "LMI Level E"}]},
        "description": (
            "Which missile classification this impact ran under — SMI, LMI Level D "
            "or LMI Level E. Chosen in LabOS and published with the result; for a "
            "job imported from Airtable the SMI/LMI half comes from the section's "
            "IMPACT_SMI / IMPACT_LMI requirement code and only the level is the "
            "operator's. Populated for Test Type = Impact, blank on every other "
            "type. Replaces the Missile Type, Missile Weight and Impact Velocity "
            "request of 2026-09-08, which LabOS no longer reads."),
    }),
    # --- the per-standard result fields, 2026-09-11 -------------------------
    #
    # Requested and then withdrawn on 2026-09-06 under A9 - "no dedicated
    # scalar; add one only when a named operational report requires it". The
    # product owner reopened it on 2026-09-10: Forced Entry and ANSI are judged
    # under different standards and their results must be distinguishable.
    #
    # The shared `Test Result` is unchanged and still carries every attempt's
    # verdict; these are additional, and each is blank on the four types it
    # does not apply to. Same four options and the same Passed/Failed spelling
    # the base already uses, because this is the same value projected by type,
    # not a second vocabulary.
    (RAW_RESULTS, {
        "name": "Forced Entry Result",
        "type": "singleSelect",
        "options": {"choices": [{"name": "Pending"}, {"name": "Passed"},
                                {"name": "Failed"}, {"name": "Inconclusive"}]},
        "description": (
            "The Forced Entry verdict, for filtering and reporting on that standard "
            "alone. Populated only for Test Type = Forced Entry and blank on every "
            "other type. Pending until the first review, then the verdict - the same "
            "value as Test Result, which is unchanged and still carries it for all "
            "five test types."),
    }),
    (RAW_RESULTS, {
        "name": "ANSI Result",
        "type": "singleSelect",
        "options": {"choices": [{"name": "Pending"}, {"name": "Passed"},
                                {"name": "Failed"}, {"name": "Inconclusive"}]},
        "description": (
            "The ANSI Z97.1 verdict, for filtering and reporting on that standard "
            "alone. Populated only for Test Type = ANSI Z97.1 and blank on every "
            "other type. Pending until the first review, then the verdict - the same "
            "value as Test Result, which is unchanged and still carries it for all "
            "five test types."),
    }),
    (RAW_RESULTS, {
        "name": "Target Impact Velocity",
        "type": "number",
        "options": {"precision": 2},
        "description": (
            "The target impact velocity the test was run against, in ft/s, entered "
            "in LabOS by the operator. A target, never a measurement: the achieved "
            "velocity of each individual impact stays in LabOS and travels only "
            "inside Complete LabOS JSON Response. Populated for Test Type = Impact, "
            "blank on every other type."),
    }),

    # --- LabOS Raw Data Table: corrections, review identity, execution times, evidence
    (RAW_RESULTS, {
        "name": "Corrects Attempt ID",
        "type": "singleLineText",
        "description": (
            "On a correction row, the LabOS Attempt ID of the attempt it supersedes. The "
            "original row is never edited or deleted. Roll-ups must exclude superseded "
            "attempts and must not count a correction as an extra physical test."),
    }),
    (RAW_RESULTS, {
        "name": "LabOS Verdict By",
        "type": "singleLineText",
        "description": (
            "Declared identity of the reviewer who set Test Result. Stored separately from "
            "Operator Name even when it is the same person, because running a test and "
            "judging it are different acts. Identity is declared, not authenticated."),
    }),
    (RAW_RESULTS, {
        "name": "LabOS Verdict At",
        "type": "dateTime",
        "options": copy.deepcopy(ISO_DATETIME),
        "description": (
            "UTC instant of the first review. Written once, with the verdict. A later "
            "correction records its own time on its own row and leaves this one alone."),
    }),
    (RAW_RESULTS, {
        "name": "Testing Start Date",
        "type": "dateTime",
        "options": copy.deepcopy(ISO_DATETIME),
        "description": (
            "UTC instant physical execution started. Sent when the attempt is created. "
            "Test Date remains the completion instant - one field could not carry both, "
            "which is why start and end are explicit."),
    }),
    (RAW_RESULTS, {
        "name": "Testing End Date",
        "type": "dateTime",
        "options": copy.deepcopy(ISO_DATETIME),
        "description": (
            "UTC instant execution completed or was aborted. Absent while a test is "
            "running. A correction preserves the ORIGINAL execution times rather than "
            "stamping the moment the correction was written."),
    }),
    (RAW_RESULTS, {
        "name": "LabOS Photos",
        "type": "multipleAttachments",
        "description": (
            "Downscaled JPEG previews of test photographs. Full-resolution originals stay "
            "in LabOS and are immutable. Each preview has a stable artifact ID, content "
            "hash and deterministic filename so an uncertain upload can be reconciled "
            "rather than blindly re-appended."),
    }),
]


def load_env(path):
    env = {}
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
                if m:
                    env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def api(method, url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_tables(base_id, token):
    return api("GET", f"https://api.airtable.com/v0/meta/bases/{base_id}/tables", token)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", required=True,
                    help="where before/after schema and the result log are written")
    ap.add_argument("--apply", action="store_true",
                    help="actually create fields; without it nothing is written")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args(argv)

    env = {**load_env(args.env), **os.environ}
    token = env.get("AIRTABLE_TOKEN")
    if not token:
        raise SystemExit("no AIRTABLE_TOKEN")

    # Rule 1. Not overridable.
    if TESTING_BASE == PRODUCTION_BASE or env.get("AIRTABLE_BASE_ID") == PRODUCTION_BASE:
        raise SystemExit("refusing: production base is never a target of this script")

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    before = fetch_tables(TESTING_BASE, token)
    with open(os.path.join(args.out_dir, f"before-{stamp}.json"), "w") as fh:
        json.dump(before, fh, indent=1)

    existing = {}
    for t in before["tables"]:
        for f in t["fields"]:
            existing[(t["id"], f["name"])] = f
    names = {t["id"]: t["name"] for t in before["tables"]}

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode}  base={TESTING_BASE} (testing)  {len(FIELDS)} decided fields\n")

    log = []
    for table_id, spec in FIELDS:
        label = f"{names.get(table_id, table_id):<20} {spec['name']:<24}"
        if (table_id, spec["name"]) in existing:
            fid = existing[(table_id, spec["name"])]["id"]
            print(f"  SKIP    {label} already exists  {fid}")
            log.append({"table": names.get(table_id), "field": spec["name"],
                        "action": "skipped", "reason": "already exists", "field_id": fid})
            continue
        if not args.apply:
            opts = spec.get("options", {})
            detail = ""
            if "choices" in opts:
                detail = " [" + ", ".join(c["name"] for c in opts["choices"]) + "]"
            print(f"  CREATE  {label} {spec['type']}{detail}")
            log.append({"table": names.get(table_id), "field": spec["name"],
                        "action": "would create", "type": spec["type"]})
            continue
        try:
            created = api(
                "POST",
                f"https://api.airtable.com/v0/meta/bases/{TESTING_BASE}/tables/{table_id}/fields",
                token, spec)
            print(f"  OK      {label} created  {created['id']}")
            log.append({"table": names.get(table_id), "field": spec["name"],
                        "action": "created", "type": spec["type"],
                        "field_id": created["id"]})
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:400]
            print(f"  FAIL    {label} {e.code} {body}")
            log.append({"table": names.get(table_id), "field": spec["name"],
                        "action": "failed", "error": f"{e.code} {body}"})

    if args.apply:
        after = fetch_tables(TESTING_BASE, token)
        with open(os.path.join(args.out_dir, f"after-{stamp}.json"), "w") as fh:
            json.dump(after, fh, indent=1)
        n_before = sum(len(t["fields"]) for t in before["tables"])
        n_after = sum(len(t["fields"]) for t in after["tables"])
        print(f"\nfields: {n_before} -> {n_after}  (+{n_after - n_before})")

    with open(os.path.join(args.out_dir, f"changes-{stamp}.json"), "w") as fh:
        json.dump({"mode": mode, "base": TESTING_BASE, "utc": stamp, "changes": log},
                  fh, indent=1)
    print(f"\nevidence -> {args.out_dir}")
    if not args.apply:
        print("nothing was written. re-run with --apply to create these fields.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
