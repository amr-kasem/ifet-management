"""Stage 3 — the one live write round-trip, with the fourteen checks.

    # see every payload, make ZERO network calls (the default)
    python3 -m tests.stage3_live_write

    # actually run it, once the Airtable team has said yes
    python3 -m tests.stage3_live_write --live --approved-by "Luis Macias 2026-09-02"

This is the last verification step before the sync worker is built. It proves the
guarantee the whole integration rests on: **a retried write can never duplicate a
test result.**

Origin: `labos-airtable-verification-report-2026-07-29.md` §4 stage 3 described
fourteen checks against a payload that is now retired — it targeted the sandbox
`appYBTqIL43pmS0xN`, the table name `LabOS Raw Test Results`, and the field pair
`Testing Start Date` / `Testing End Date`, none of which exist any more. The
fourteen checks still stand; this re-points them at the live target from contract
v0.3 §0.1: `app4oXS3Kd5IKWgJ7` / `tblnc9SsbXU0C0FWh`, upserting on
`LabOS Attempt ID`.

Four safety properties, in order of how much they matter:

1. **The production base is refused unconditionally.** Not gated behind a flag —
   refused. `AIRTABLE_ALLOW_PRODUCTION_WRITE` does not help you here.
2. **Dry run is the default.** Without `--live` nothing leaves the process, and
   every payload is printed for review first.
3. **`--approved-by` is mandatory for a live run** and is echoed into the run
   header, so the record of who authorised it lives with the result.
4. **Every write reuses one `LabOS Attempt ID`.** The deliberately-malformed
   payloads in checks 5-10 are upserts on that same key, so if Airtable accepts
   something we expected it to reject, it merges onto the probe row rather than
   littering the table with junk records.

Housekeeping: every row is tagged `Operator Name = LABOS-PROBE` with a
`LabOS Test ID` starting `probe-`. LabOS never deletes, so ask the Airtable team
to purge tagged rows afterwards.
"""

import argparse
import datetime as dt
import json
import sys
import time
import uuid

from app.airtable import contract as C
from app.airtable import envelope
from app.airtable.client import AirtableClient, MAX_BATCH
from app.airtable.errors import AirtableError, AirtableValidationError
from app.config import BASE_PRODUCTION, BASE_TESTING, TABLE_RAW_DATA, airtable_settings

MERGE_KEY = "LabOS Attempt ID"
PROBE_OPERATOR = "LABOS-PROBE"


# --------------------------------------------------------------------- report

class Report:
    """Collects check outcomes so the exit code reflects what actually happened."""

    def __init__(self):
        self.rows = []

    def record(self, number, group, name, ok, detail=""):
        self.rows.append((number, group, name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {number:>2}. {name}")
        if detail:
            for line in str(detail).splitlines():
                print(f"          {line}")

    @property
    def failures(self):
        return [r for r in self.rows if not r[3]]

    def summary(self):
        print()
        print("=" * 72)
        passed = len(self.rows) - len(self.failures)
        print(f"stage 3: {passed}/{len(self.rows)} checks passed")
        for number, group, name, ok, detail in self.failures:
            print(f"  FAILED {number}. [{group}] {name}")
        return 1 if self.failures else 0


# ------------------------------------------------------------------- payloads

def probe_values(attempt_id, test_id, *, started, ended):
    """A complete, contract-valid Static Load attempt.

    Static Load is not a stylistic choice: the live `Test Type` option set holds
    only that one value (§10.17), so it is the only type Airtable will accept at
    all until the other four options are added.
    """
    return {
        "Airtable Project ID": "recPROBEproject01",
        "Airtable Mock-Up ID": "recPROBEmockup001",
        "Airtable Protocol ID": "recPROBEprotocol1",
        "Airtable Section ID": "recPROBEsection01",
        "LabOS Test ID": test_id,
        "LabOS Attempt ID": attempt_id,
        "Attempt Number": 1,
        "Test Name": "Stage 3 probe — static load",
        "Test Type": C.STATIC_LOAD,
        "Test Result": "Pass",          # translated to their spelling at the wire
        "Measured Value": 40.0,
        "Unit": "PSF",
        "Max Pressure Achieved": 62.5,
        "Deflection Value": 0.42,
        # Deliberately OUR vocabulary, not theirs, and this is not an oversight.
        #
        # Their sample row recxZWiVa5Wuy0ZV6 (2026-08-10) writes "Inches" here.
        # The live field is singleLineText, so Airtable would accept either and
        # leave two spellings of one unit in one column, silently. Contract
        # §10.24 asks them to settle it.
        #
        # We do not pre-empt that answer, for two reasons. Their row is a hand
        # written sample, not necessarily a considered vocabulary; and switching
        # only this field to "Inches" would leave `Unit` accepting "in" while
        # `Deflection Unit` required "Inches" -- internally inconsistent, which
        # is worse than being consistently different from them.
        #
        # It is safe to leave as-is because probe rows are tagged LABOS-PROBE and
        # are purged. What must NOT happen is a real sync starting before §10.24
        # is answered: `envelope.build` refuses "Inches" outright (verified), so
        # their current spelling is not merely untidy, it is unsendable.
        "Deflection Unit": "in",
        "Required Value": 60.0,
        "Required Unit": "PSF",
        "Result Detail (JSON)": {
            "load_steps": [{"psf": 20, "held_s": 10}, {"psf": 40, "held_s": 10}],
            "gauges": {"g1": 0.42, "g2": 0.31, "g3": 0.00},
        },
        "Operator Name": PROBE_OPERATOR,
        "Retest Required": False,
        "Testing Continued": "Continued",
        "Testing Start Date": started,
        "Testing End Date": ended,
        "LabOS Created At": started,
        "LabOS Updated At": ended,
    }


# --------------------------------------------------------------------- checks

def run(client, table, report, *, live, live_options):
    started = dt.datetime(2026, 9, 2, 14, 3, 0, tzinfo=dt.timezone.utc)
    ended = dt.datetime(2026, 9, 2, 14, 31, 0, tzinfo=dt.timezone.utc)
    attempt_id = f"probe-attempt-{uuid.uuid4()}"
    test_id = f"probe-test-{uuid.uuid4()}"

    values = probe_values(attempt_id, test_id, started=started, ended=ended)

    # -- 1 ---------------------------------------------------------- offline
    try:
        record = envelope.build_terminal(values, live_options=live_options)
        ok = record.get(MERGE_KEY) == attempt_id and "Test Date" in record
        report.record(1, "envelope", "payload builds and carries the merge key", ok,
                      "" if ok else f"built: {sorted(record)}")
    except Exception as exc:                                  # noqa: BLE001
        report.record(1, "envelope", "payload builds and carries the merge key",
                      False, f"{type(exc).__name__}: {exc}")
        print("\n  Cannot continue: the payload does not build. Nothing was sent.")
        return

    print()
    print("  payload that will be sent (wire names):")
    for k in sorted(record):
        shown = record[k]
        if isinstance(shown, str) and len(shown) > 90:
            shown = shown[:87] + "..."
        print(f"    {k:<34} {shown!r}")
    print()

    if not live:
        print("  DRY RUN — stopping here. Checks 2-14 need --live.")
        return

    # -- 2, 3, 4 ------------------------------------------- the core guarantee
    first = client.upsert_records(table, [record], merge_on=(MERGE_KEY,))
    ids_first = [r["id"] for r in first.get("records", [])]
    report.record(2, "no duplicates", "first upsert creates exactly one record",
                  len(ids_first) == 1, f"record id: {ids_first}")

    second = client.upsert_records(table, [record], merge_on=(MERGE_KEY,))
    ids_second = [r["id"] for r in second.get("records", [])]
    report.record(3, "no duplicates", "identical re-send updates, does not create",
                  ids_second == ids_first,
                  f"first={ids_first} second={ids_second}")

    third = client.upsert_records(table, [record], merge_on=(MERGE_KEY,))
    ids_third = [r["id"] for r in third.get("records", [])]
    matches = [r for r in client.iter_records(
        table, formula=f"{{{MERGE_KEY}}} = '{attempt_id}'")]
    report.record(4, "no duplicates",
                  "replayed retry still leaves exactly one row in the table",
                  ids_third == ids_first and len(matches) == 1,
                  f"rows carrying this attempt id: {len(matches)}")

    # -- 5, 6, 7 ------------------------------------------- blank handling
    # Deliberately malformed. Sent as upserts on the SAME merge key, so an
    # unexpected acceptance merges onto the probe row instead of creating junk.
    for number, field, bad, kind in (
        (5, "Measured Value", "", "number"),
        (6, "Test Date", "", "date"),
        (7, "Test Status", "", "single select"),
    ):
        payload = dict(record)
        payload[field] = bad
        rejected, detail = _expect_rejection(client, table, payload)
        report.record(number, "blank handling",
                      f"empty string to a {kind} field is rejected", rejected, detail)

    # -- 8 ------------------------------------------------- omission accepted
    payload = {k: v for k, v in record.items() if k != "Deflection Value"}
    try:
        client.upsert_records(table, [payload], merge_on=(MERGE_KEY,))
        report.record(8, "blank handling",
                      "omitting the key instead is accepted", True,
                      "confirms the contract's omit-don't-blank rule is the right one")
    except AirtableError as exc:
        report.record(8, "blank handling", "omitting the key instead is accepted",
                      False, f"{type(exc).__name__}: {exc}")

    # -- 9 ------------------------------------------- unknown option refused
    payload = dict(record)
    payload["Test Type"] = "Definitely Not A Real Test Type"
    rejected, detail = _expect_rejection(client, table, payload)
    report.record(9, "value discipline",
                  "unknown select option fails loudly, no stray option created",
                  rejected, detail)

    # -- 10 ------------------------------------------------ a genuine zero
    payload = dict(record)
    payload["Deflection Value"] = 0
    client.upsert_records(table, [payload], merge_on=(MERGE_KEY,))
    back = client.get_record(table, ids_first[0])["fields"]
    stored = back.get("Deflection Value")
    report.record(10, "value discipline", "a genuine 0 is stored as 0, not as blank",
                  stored == 0, f"read back: {stored!r}")

    # -- 11 --------------------------------------- batch limit, client-side
    try:
        client.upsert_records(table, [record] * (MAX_BATCH + 1), merge_on=(MERGE_KEY,))
        report.record(11, "limits", f"batch over {MAX_BATCH} is refused before sending",
                      False, "the client sent it — the guard did not fire")
    except ValueError as exc:
        report.record(11, "limits", f"batch over {MAX_BATCH} is refused before sending",
                      True, str(exc))

    # -- 12 ---------------------------------------------- rate limiting holds
    began = time.monotonic()
    for _ in range(6):
        client.list_records(table, page_size=1)
    elapsed = time.monotonic() - began
    report.record(12, "limits", "client throttles to Airtable's 5 req/s per base",
                  elapsed >= 1.0,
                  f"6 requests took {elapsed:.2f}s (expected >= 1.0s)")

    # -- 13 ------------------------------------------------ JSON round-trip
    back = client.get_record(table, ids_first[0])["fields"]
    json_field = C.BY_LABOS_NAME["Result Detail (JSON)"].wire_name
    try:
        restored = json.loads(back.get(json_field, "{}"))
        gauges_ok = restored.get("gauges", {}).get("g3") == 0.00
        steps_ok = len(restored.get("load_steps", [])) == 2
        report.record(13, "formats", "structured detail survives the round-trip intact",
                      gauges_ok and steps_ok,
                      f"gauges g3={restored.get('gauges', {}).get('g3')!r}, "
                      f"load_steps={len(restored.get('load_steps', []))}")
    except ValueError as exc:
        report.record(13, "formats", "structured detail survives the round-trip intact",
                      False, f"stored value is not valid JSON: {exc}")

    # -- 14 -------------------------------------------- timestamp round-trip
    # Their `Test Date` is date-only (§10.20), so the time of day and the end
    # timestamp can only survive inside the JSON valve. This checks that they do,
    # and that duration is still derivable.
    try:
        restored = json.loads(back.get(json_field, "{}"))
        extra = restored.get("labos_extra", {})
        duration_ok = extra.get("duration_s") == 1680
        end_ok = extra.get("testing_end_date", "").startswith("2026-09-02T14:31")
        date_ok = str(back.get("Test Date", "")).startswith("2026-09-02")
        report.record(14, "formats",
                      "UTC timestamps round-trip; duration still derivable",
                      duration_ok and end_ok and date_ok,
                      f"Test Date={back.get('Test Date')!r} "
                      f"end={extra.get('testing_end_date')!r} "
                      f"duration_s={extra.get('duration_s')!r}")
    except ValueError as exc:
        report.record(14, "formats",
                      "UTC timestamps round-trip; duration still derivable",
                      False, str(exc))

    print()
    print(f"  probe rows are tagged Operator Name = {PROBE_OPERATOR!r}")
    print(f"  LabOS Test ID  = {test_id}")
    print(f"  Airtable row   = {ids_first[0] if ids_first else '(none)'}")
    print("  LabOS never deletes — ask the Airtable team to purge tagged rows.")


def _expect_rejection(client, table, payload):
    """Send something we expect Airtable to refuse. Report what it actually did."""
    try:
        client.upsert_records(table, [payload], merge_on=(MERGE_KEY,))
    except AirtableValidationError as exc:
        return True, f"rejected with 422, as expected: {exc}"
    except AirtableError as exc:
        return True, f"rejected ({type(exc).__name__}): {exc}"
    return False, ("ACCEPTED — Airtable did not reject it. This is a finding: it "
                   "means the base tolerates a value the contract forbids, and "
                   "the omit-don't-blank rule is load-bearing on our side alone.")


# ---------------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stage 3 — one live write round-trip into the TESTING base")
    parser.add_argument("--live", action="store_true",
                        help="actually send. Without this nothing leaves the process.")
    parser.add_argument("--approved-by", metavar="WHO",
                        help="who approved this run, and when. Required with --live.")
    args = parser.parse_args(argv)

    settings = airtable_settings
    table = settings.results_table or TABLE_RAW_DATA

    print("=" * 72)
    print("Stage 3 — live write round-trip")
    print("=" * 72)
    print(f"  base   : {settings.base_id}")
    print(f"  table  : {table}")
    print(f"  mode   : {'LIVE — this will write' if args.live else 'DRY RUN — no network calls'}")
    if args.approved_by:
        print(f"  approved by: {args.approved_by}")
    print()

    # Safety 1 — the production base is refused outright, flag or no flag.
    if settings.base_id == BASE_PRODUCTION:
        print("REFUSED: this script never writes the production base "
              f"({BASE_PRODUCTION}). Stage 3 runs against the testing base "
              f"({BASE_TESTING}) only. Set AIRTABLE_BASE_ID and try again.",
              file=sys.stderr)
        return 2

    if args.live:
        if not settings.token:
            print("REFUSED: no AIRTABLE_TOKEN set.", file=sys.stderr)
            return 2
        # Safety 3 — a live run must name its authorisation.
        if not args.approved_by:
            print("REFUSED: --live needs --approved-by \"<who, and when>\". "
                  "This run writes to the Airtable team's base; the approval "
                  "belongs in the record next to the result.", file=sys.stderr)
            return 2

    client = AirtableClient()

    live_options = None
    if args.live:
        # Translate select values against what the base actually holds today,
        # rather than against what the contract wishes it held.
        schema = client.get_base_schema()
        for t in schema.get("tables", []):
            if t["id"] == table:
                live_options = {
                    f["name"]: tuple(
                        o["name"] for o in f.get("options", {}).get("choices", []))
                    for f in t.get("fields", [])
                    if f.get("options", {}).get("choices")
                }
                break

    report = Report()
    run(client, table, report, live=args.live, live_options=live_options)
    return report.summary() if report.rows else 0


if __name__ == "__main__":
    sys.exit(main())
