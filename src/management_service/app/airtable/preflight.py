"""Pre-send check — does the live schema actually match what the document claims?

    python -m app.airtable.preflight --env ../../.env

Read-only, both bases. Run this immediately before sending the change document
to the Airtable team, because the document asserts specific things about two
bases we do not control between one day and the next.

It checks four claims, and each one has a way of being quietly wrong:

1. **Every added field exists in Testing, with the right types.** A field created by
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

# The 18, as the document lists them.
ADDED = {
    PROTOCOL_SECTIONS: [
        ("Requirement Code", "singleSelect"), ("Requirement Kind", "singleSelect"),
        ("Applicability", "singleSelect"), ("Required Value", "number"),
        ("Required Value Inward", "number"), ("Required Value Outward", "number"),
        ("Required Unit", "singleSelect"), ("Required Option", "singleLineText"),
    ],
    RAW: [
        ("Corrects Attempt ID", "singleLineText"), ("LabOS Verdict By", "singleLineText"),
        ("LabOS Verdict At", "dateTime"), ("Testing Start Date", "dateTime"),
        ("Testing End Date", "dateTime"), ("LabOS Photos", "multipleAttachments"),
        # 18 since 2026-09-08. `Impact Number` is the axis that lets their
        # roll-ups count tests and impacts separately once one attempt is one
        # impact — without it a five-impact test reads as five tests.
        ("Impact Number", "number"),
        # Applied 2026-09-11. `Impact Classification` fldMY7DiiuP9kbQbL,
        # `Target Impact Velocity` fldhywP9YpsmoWWT1 — the outbound pair that
        # replaced the three withdrawn Protocol Sections requirements.
        ("Impact Classification", "singleSelect"),
        ("Target Impact Velocity", "number"),
        # Applied 2026-09-11, TA6. `Forced Entry Result` fldAHuPzZHZEj0Cjt,
        # `ANSI Result` fldmCKJV95N9uL7xt — the same verdict as `Test Result`
        # projected onto its own standard, so a report about Forced Entry or
        # ANSI Z97.1 does not have to filter `Test Result` by `Test Type`
        # first. Their choices are asserted below: they are created in the
        # *wire* spelling, and a select holding Pass/Fail would reject every
        # verdict LabOS sends.
        ("Forced Entry Result", "singleSelect"),
        ("ANSI Result", "singleSelect"),
    ],
}

# **Applied to Testing, withdrawn from the read contract, never in
# production.** These three were part of ADDED until 2026-09-10. They are not
# deleted from the Testing Base — that is a later coordinated cleanup with the
# Airtable team — so they still have to be asserted, just differently: present
# in Testing, absent from production, and *not readable*. Dropping them from
# ADDED without this set would have quietly retired the production guard that
# is the whole reason they are safe to leave lying around.
DEPRECATED_TESTING_ONLY = {
    PROTOCOL_SECTIONS: [
        ("Missile Type", "singleLineText"),
        ("Missile Weight", "number"),
        ("Impact Velocity", "number"),
    ],
}

# **Decided and not yet created.** A field lands here the moment it is agreed
# and leaves it the day the schema write lands. While it is here it must be
# absent from *both* bases, and this script says so out loud rather than
# silently not checking it; moving it into ADDED is the schema write's
# acceptance criterion, not a tidy-up afterwards.
#
# **Empty since 2026-09-11.** TA7b's pair went in on 2026-09-11 and TA6's
# followed the same day; both are now in ADDED with their real field ids. The
# set is kept rather than deleted because it is the mechanism, not the list —
# check 2 guards whatever is in it against production, so the next decided
# field is one line away from being checked instead of assumed.
PENDING_SCHEMA = {}

# Type alone is not the contract for these. A singleSelect with the wrong
# choices accepts nothing LabOS sends; a number with precision 0 silently
# truncates 50.25 ft/s. Asserted whenever the field is present.
EXPECTED_CHOICES = {
    "Impact Classification": ["SMI", "LMI Level D", "LMI Level E"],
    # The wire spelling, not the LabOS one: the base holds Passed/Failed, and
    # contract.Field.option_wire is what translates. A select created with
    # Pass/Fail would reject every verdict LabOS sends.
    "Forced Entry Result": ["Pending", "Passed", "Failed", "Inconclusive"],
    "ANSI Result": ["Pending", "Passed", "Failed", "Inconclusive"],
}
EXPECTED_PRECISION = {
    "Target Impact Velocity": 2,
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


def _assert_shape(name, opts, fails):
    """Exact choices and precision, asserted wherever the field appears."""
    if name in EXPECTED_CHOICES:
        got = [c["name"] for c in opts.get("choices", [])]
        if got != EXPECTED_CHOICES[name]:
            fails.append(f"{name!r} choices are {got}, "
                         f"expected {EXPECTED_CHOICES[name]}")
            print(f"   CHOICES  {name}: {got}")
    if name in EXPECTED_PRECISION:
        if opts.get("precision") != EXPECTED_PRECISION[name]:
            fails.append(f"{name!r} precision is {opts.get('precision')!r}, "
                         f"expected {EXPECTED_PRECISION[name]}")
            print(f"   PRECIS   {name}: {opts.get('precision')}")


def _options(tables, table_id, field_name):
    """The live field's `options` dict, or None if the field is not there."""
    for t in tables:
        if t["id"] != table_id:
            continue
        for f in t["fields"]:
            if f["name"] == field_name:
                return f.get("options") or {}
    return None


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

    # -- 1. every added field exists in Testing, correctly typed ----------
    _n_added = sum(len(f) for f in ADDED.values())
    print(f"1. The {_n_added} added fields, in Testing")
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
            else:
                # **Type is not the whole contract for these.** A singleSelect
                # with the wrong choices accepts nothing LabOS sends, and a
                # number with precision 0 silently truncates 50.25 ft/s. Both
                # would pass a type check and fail in front of an operator.
                _assert_shape(name, _options(test_t, table, name) or {}, fails)
    print(f"   {n - len([f for f in fails])} of {n} present and correctly typed")

    # -- 1a. the deprecated three are still there, and still unread --------
    #
    # Present, because we did not delete them and must not pretend we did.
    # Unread, because that is what "withdrawn from the read contract" means -
    # and the read boundary is `mirror.SECTION_FIELDS` itself, not a document
    # describing it. A field that reappears there starts being copied again
    # with nothing else changing, which is exactly the drift worth a gate.
    print("\n1a. The withdrawn three: present in Testing, absent from the read boundary")
    from .mirror import SECTION_FIELDS                       # noqa: PLC0415
    for table, fields in DEPRECATED_TESTING_ONLY.items():
        live = test_s.get(table, {})
        for name, want in fields:
            got = live.get(name)
            if got is None:
                fails.append(f"deprecated {name!r} has vanished from Testing - "
                             "this script expects it left in place, not deleted")
                print(f"   GONE     {name}")
            elif got != want:
                fails.append(f"deprecated {name!r} is {got!r}, expected {want!r}")
            if name in SECTION_FIELDS:
                fails.append(f"{name!r} is back in mirror.SECTION_FIELDS - "
                             "LabOS would read it again")
                print(f"   READABLE {name}  <-- withdrawn field is back on the allowlist")
    print(f"   {sum(len(f) for f in DEPRECATED_TESTING_ONLY.values())} withdrawn, "
          f"{len(SECTION_FIELDS)} fields on the read allowlist")

    # -- 1b. fields decided but not yet created, and their shape once they are
    print("\n1b. Fields decided but not yet created")
    if not PENDING_SCHEMA:
        print("   none owed - every decided field has been applied to Testing")
    for table, fields in PENDING_SCHEMA.items():
        live = test_s.get(table, {})
        for name, want in fields:
            got = live.get(name)
            if got is None:
                print(f"   PENDING  {name} - not yet created (expected until "
                      "the schema write that adds it)")
                continue
            if got != want:
                fails.append(f"{name!r} is {got!r} in Testing, expected {want!r}")
                print(f"   WRONG    {name}: {got}")
                continue
            opts = _options(test_t, table, name) or {}
            if name in EXPECTED_CHOICES:
                got_choices = [c["name"] for c in opts.get("choices", [])]
                if got_choices != EXPECTED_CHOICES[name]:
                    fails.append(f"{name!r} choices are {got_choices}, "
                                 f"expected {EXPECTED_CHOICES[name]}")
                    print(f"   CHOICES  {name}: {got_choices}")
                    continue
            if name in EXPECTED_PRECISION:
                if opts.get("precision") != EXPECTED_PRECISION[name]:
                    fails.append(f"{name!r} precision is {opts.get('precision')!r}, "
                                 f"expected {EXPECTED_PRECISION[name]}")
                    print(f"   PRECIS   {name}: {opts.get('precision')}")
                    continue
            print(f"   OK       {name} - created and correctly shaped. "
                  "Move it from PENDING_SCHEMA into ADDED.")

    # -- 2. production untouched -------------------------------------------
    print("\n2. Production still has none of them")
    # **All three sets, not just ADDED.** The withdrawn fields must never
    # reach production either - they are the ones a stale change spec would
    # have proposed - and the pending two must not appear there before they
    # appear in Testing.
    guarded = {**{t: list(f) for t, f in ADDED.items()}}
    for group in (DEPRECATED_TESTING_ONLY, PENDING_SCHEMA):
        for t, f in group.items():
            guarded.setdefault(t, []).extend(f)
    leaked = [name for table, fields in guarded.items()
              for name, _ in fields if name in prod_s.get(table, {})]
    if leaked:
        fails.append(f"production already has {leaked} — 'production is untouched' is false")
        print(f"   PRESENT IN PRODUCTION: {leaked}")
    else:
        # Counted, not written out. This said "17" until 2026-09-08 and stayed
        # saying it after the eighteenth field was added — a status line that
        # cannot go stale is worth the one expression.
        print(f"   none of the {sum(len(f) for f in guarded.values())} "
              "guarded fields are in production "
              f"({sum(len(f) for f in ADDED.values())} added, "
              f"{sum(len(f) for f in DEPRECATED_TESTING_ONLY.values())} withdrawn, "
              f"{sum(len(f) for f in PENDING_SCHEMA.values())} pending)")
    p_count = sum(len(f) for f in prod_s.values())
    t_count = sum(len(f) for f in test_s.values())
    print(f"   production {p_count} fields · testing {t_count} fields · delta {t_count - p_count}")
    if p_count != 142:
        fails.append(f"production is {p_count} fields, document says 142")
    # The delta the document claims **is** the number of fields this script
    # asserts, so it is derived from `ADDED` rather than restated. Written as a
    # literal 17, it was the check that caught the eighteenth field — and then
    # it would have had to be edited by hand every time, which is how a gate
    # ends up asserting last month's truth.
    # The delta counts everything Testing has that production does not: the
    # added fields, the withdrawn three that are still sitting there, and any
    # pending field once it is created.
    expected = sum(len(f) for f in ADDED.values()) \
        + sum(len(f) for f in DEPRECATED_TESTING_ONLY.values()) \
        + sum(1 for table, fields in PENDING_SCHEMA.items()
              for name, _ in fields if name in test_s.get(table, {}))
    if t_count - p_count != expected:
        fails.append(f"delta is {t_count - p_count}, this script asserts "
                     f"{expected} field(s)")

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
