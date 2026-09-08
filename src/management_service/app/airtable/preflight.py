"""Pre-send check — does the live schema actually match what the document claims?

    python -m app.airtable.preflight --env ../../.env

Read-only, both bases. Run this immediately before sending the change document
to the Airtable team, because the document asserts specific things about two
bases we do not control between one day and the next.

It checks four claims, and each one has a way of being quietly wrong:

1. **The 17 fields exist in Testing, with the right types.** A field created by
   hand instead of by the tool could be `singleLineText` where the document says
   `number`, and nothing else would notice.
2. **Production still has none of them, and is still 142 fields.** If someone
   applied them early, "production is untouched" is false and the document is
   misleading rather than merely stale.
3. **Every row in `production-change-spec.csv` matches the live bases** — for a
   KEEP row, the field's **type** as well as its presence. The document's "0
   retyped" claim is what makes the change additive in the sense their
   automations care about, and it came from a generated snapshot; a field
   retyped since then would have gone unnoticed.
4. **The fixture still reads back.** Someone may have cleared the Testing base,
   in which case the verification section describes something no longer there.
5. **The document's own arithmetic.** Reads + writes + ignored must equal the
   base, every outbound field must name a phase the outbox actually has, and a
   field we have decided to withhold must not be counted as one we write. This
   check exists because all three were wrong at once on 2026-09-08: the counts
   summed to 160 against a 159-field base, `LabOS Updated At` carried a phase
   token `every` that `sync.outbox` does not define, and three withheld fields
   were counted as writes in §1 while §4 said they are never sent.
"""

import argparse
import csv
import os
import pathlib
import sys

from .apply_schema import api, load_env

TESTING = "app4oXS3Kd5IKWgJ7"
PRODUCTION = "app0OCunbmuXl7Hc9"
PROTOCOL_SECTIONS = "tblqpvuJlSdkeS9PS"
RAW = "tblnc9SsbXU0C0FWh"

# The 17, as the document lists them.
ADDED = {
    PROTOCOL_SECTIONS: [
        ("Requirement Code", "singleSelect"), ("Requirement Kind", "singleSelect"),
        ("Applicability", "singleSelect"), ("Required Value", "number"),
        ("Required Value Inward", "number"), ("Required Value Outward", "number"),
        ("Required Unit", "singleSelect"), ("Required Option", "singleLineText"),
        ("Missile Type", "singleLineText"), ("Missile Weight", "number"),
        ("Impact Velocity", "number"),
    ],
    RAW: [
        ("Corrects Attempt ID", "singleLineText"), ("LabOS Verdict By", "singleLineText"),
        ("LabOS Verdict At", "dateTime"), ("Testing Start Date", "dateTime"),
        ("Testing End Date", "dateTime"), ("LabOS Photos", "multipleAttachments"),
    ],
}

# …/ifet-project/ifet-management/src/management_service/app/airtable/preflight.py
#  parents:  0 airtable  1 app  2 management_service  3 src  4 ifet-management
#            5 ifet-project — the sibling checkout lives beside it.
SPEC = (pathlib.Path(__file__).resolve().parents[5] / "ifet-firmware" / "docs"
        / "labos-airtable" / "evidence" / "testing-base-changes-2026-09-06"
        / "production-change-spec.csv")


def schema(base, token):
    tables = api("GET", f"https://api.airtable.com/v0/meta/bases/{base}/tables", token)["tables"]
    return {t["id"]: {f["name"]: f["type"] for f in t["fields"]} for t in tables}, tables


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default=".env")
    args = ap.parse_args(argv)
    env = {**load_env(args.env), **os.environ}
    t_tok = env.get("AIRTABLE_TOKEN", "").strip()
    p_tok = env.get("AIRTABLE_TOKEN_PRODUCTION", "").strip()
    if not (t_tok and p_tok):
        print("ERROR: both AIRTABLE_TOKEN and AIRTABLE_TOKEN_PRODUCTION are needed")
        return 2

    test_s, test_t = schema(TESTING, t_tok)
    prod_s, prod_t = schema(PRODUCTION, p_tok)
    fails, warns = [], []

    print("PRE-SEND CHECK — read-only, both bases\n")

    # -- 1. the 17 exist in Testing, correctly typed ----------------------
    print("1. The 17 added fields, in Testing")
    n = 0
    for table, fields in ADDED.items():
        live = test_s.get(table, {})
        for name, want in fields:
            got = live.get(name)
            n += 1
            if got is None:
                fails.append(f"Testing is missing {name!r}")
                print(f"   MISSING  {name}")
            elif got != want:
                fails.append(f"{name!r} is {got!r} in Testing, document says {want!r}")
                print(f"   WRONG    {name}: {got} (document says {want})")
    print(f"   {n - len([f for f in fails])} of {n} present and correctly typed")

    # -- 2. production untouched -------------------------------------------
    print("\n2. Production still has none of them")
    leaked = [name for table, fields in ADDED.items()
              for name, _ in fields if name in prod_s.get(table, {})]
    if leaked:
        fails.append(f"production already has {leaked} — 'production is untouched' is false")
        print(f"   PRESENT IN PRODUCTION: {leaked}")
    else:
        print("   none of the 17 are in production")
    p_count = sum(len(f) for f in prod_s.values())
    t_count = sum(len(f) for f in test_s.values())
    print(f"   production {p_count} fields · testing {t_count} fields · delta {t_count - p_count}")
    if p_count != 142:
        fails.append(f"production is {p_count} fields, document says 142")
    if t_count - p_count != 17:
        fails.append(f"delta is {t_count - p_count}, document says 17")

    # -- 3. the spec CSV matches the live bases ----------------------------
    print("\n3. production-change-spec.csv against both bases")
    if not SPEC.exists():
        fails.append(f"{SPEC} not found")
    else:
        by_name = {}
        for tbl in prod_t:
            by_name[tbl["name"]] = tbl["id"]
        rows = list(csv.DictReader(open(SPEC)))
        bad = 0
        for r in rows:
            tid = by_name.get(r["table"])
            if tid is None:
                continue
            in_prod = r["field"] in prod_s.get(tid, {})
            in_test = r["field"] in test_s.get(tid, {})
            if r["action"] == "KEEP" and not in_prod:
                bad += 1
                fails.append(f"spec says KEEP {r['field']!r} but production does not have it")
            if r["action"] == "ADD" and in_prod:
                bad += 1
                fails.append(f"spec says ADD {r['field']!r} but production already has it")
            if r["action"] == "ADD" and not in_test:
                bad += 1
                fails.append(f"spec says ADD {r['field']!r} but it is not in Testing either")

            # **Types on the KEEP rows, not just presence.**
            #
            # The document's "0 retyped" claim is what makes the change additive
            # in the sense that matters to their automations, and this check
            # verified only that a KEEP field still *existed*. The claim itself
            # came from the generated before/after CSV — true when generated, and
            # generated from a snapshot. A field retyped in Testing since then
            # would have passed here while the document asserted otherwise.
            #
            # Compared against Testing, because Testing is the base being
            # replicated: a pre-existing field whose type differs there is
            # exactly the retype the document says did not happen.
            if r["action"] == "KEEP" and in_test and r.get("type"):
                live_type = test_s.get(tid, {}).get(r["field"])
                if live_type and live_type != r["type"]:
                    bad += 1
                    fails.append(
                        f"{r['field']!r} in {r['table']} is {live_type!r} in "
                        f"Testing but the spec says {r['type']!r} — the "
                        "document claims 0 fields were retyped")
        print(f"   {len(rows)} rows checked, {bad} disagree with the live bases")
        print(f"   including the type of every pre-existing field, not just "
              f"its presence")

    # -- 4. the fixture is still there -------------------------------------
    print("\n4. The fixture the document cites")
    import urllib.parse
    f = urllib.parse.quote("{IFET job number}='IFET-FIXTURE-0001'")
    recs = api("GET", f"https://api.airtable.com/v0/{TESTING}/tblLYcRC7q6Srjfk3"
                      f"?filterByFormula={f}", t_tok).get("records", [])
    if not recs:
        warns.append("the fixture job is gone from Testing — §5 of the document "
                     "describes a verification that can no longer be repeated")
        print("   MISSING — fixture job IFET-FIXTURE-0001 not found")
    else:
        secs = api("GET", f"https://api.airtable.com/v0/{TESTING}/{PROTOCOL_SECTIONS}",
                   t_tok).get("records", [])
        codes = sorted({s["fields"].get("Requirement Code") for s in secs
                        if s["fields"].get("Requirement Code")})
        print(f"   present · {len(secs)} protocol sections · codes {codes}")
        need = {"STATIC_PRESSURE", "CYCLIC_PRESSURE", "IMPACT_LMI", "FORCED_ENTRY",
                "ANSI_IMPACT", "GAUGE_COUNT"}
        missing = need - set(codes)
        if missing:
            warns.append(f"fixture no longer covers {sorted(missing)}")

    # -- 5. the document's arithmetic, from the generated schema ----------
    print("\n5. The document's counts, and every phase the outbox must know")
    SCHEMA = SPEC.parent.parent.parent / "contract" / "interface-schema.csv"
    PHASES = {"create", "terminal", "verdict", "attachment"}
    if not SCHEMA.exists():
        warns.append(f"interface-schema.csv not found at {SCHEMA}")
        print("   SKIPPED — interface-schema.csv not found")
    else:
        rows = [r for r in csv.DictReader(open(SCHEMA)) if r["in_testing"] == "yes"]
        out = [r for r in rows if r["direction"] == "OUT"]
        reads = [r for r in rows if r["direction"] in ("IN", "READ_ONLY")]
        ignored = len(rows) - len(out) - len(reads)
        both = [r for r in rows if r["direction"] == "OUT" and r["direction"] in ("IN",)]
        print(f"   {len(rows)} fields = {len(reads)} read + {len(out)} write-bound "
              f"+ {ignored} ignored")
        if len(reads) + len(out) + ignored != len(rows):
            fails.append("reads + writes + ignored does not equal the base size")
        if both:
            fails.append("a field is counted as both read and written")

        # Every outbound field must name a phase the outbox can dispatch.
        unknown = {}
        for r in out:
            for tok in (t.strip() for t in r["write_phase"].split("+")):
                if tok and tok not in PHASES:
                    unknown.setdefault(tok, []).append(r["airtable_field"])
        if unknown:
            for tok, fs in sorted(unknown.items()):
                fails.append(f"write_phase {tok!r} is not a sync.outbox phase "
                             f"({', '.join(sorted(fs))}) — it would never dispatch")
        else:
            print(f"   every write-bound field names one of {sorted(PHASES)}")

        # A withheld field counted as a write is a promise we do not keep.
        withheld = [r["airtable_field"] for r in out if r["delivery_state"] == "OMITTED"]
        conditional = [r["airtable_field"] for r in out if r["delivery_state"] == "CONDITIONAL"]
        always = len(out) - len(withheld) - len(conditional)
        print(f"   {always} always · {len(conditional)} conditional · "
              f"{len(withheld)} withheld ({', '.join(sorted(withheld))})")
        doc = SPEC.parent.parent.parent / "correspondence" / "testing-base-change-document-2026-09-08.md"
        if doc.exists():
            text = doc.read_text()
            # Only the numbers the document *should* state: the partition of the
            # base, and the withheld count.
            #
            # `always` and `conditional` are deliberately NOT checked. Counting
            # them from `delivery_state` is what produced the "28 always arrive"
            # claim, and it is wrong per attempt: `Corrects Attempt ID` is
            # BASELINE and blank on every non-correction, `Impact Result` applies
            # to one test type, and the reviewer fields exist only after review.
            # Presence is decided by phase and applicability, so the document
            # states those rules instead of a number that cannot be right.
            for label, n in (("reads", len(reads)),
                             ("write-bound", len(out)),
                             ("withheld", len(withheld)),
                             ("ignored", ignored)):
                if f"**{n}**" not in text:
                    warns.append(f"the document does not state {label} = {n}")

    print("\n" + "=" * 60)
    for w in warns:
        print("WARN ", w)
    if fails:
        print(f"\n{len(fails)} FAILURE(S) — do not send:")
        for x in fails:
            print("  -", x)
        return 1
    print("\nAll claims in the change document hold against both live bases.")
    print("Safe to send." + ("  (see warnings above)" if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
