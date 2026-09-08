"""Stage 4 — all five test types, all three phases, against the live Testing base.

    python3 -m tests.stage4_five_types_live                      # dry run (default)
    python3 -m tests.stage4_five_types_live --live --approved-by "who, when"

Stage 3 proved the mechanism with one Static Load payload: upsert merges, an
identical re-send does not duplicate, blanks are refused, options are not
invented. It says nothing about the other four workflows, and until 2026-09-08
three of them could not resolve their Airtable identity at all — so "the sync
works" had been demonstrated for two types out of five.

This proves the part stage 3 cannot: **each of the five types publishes, and its
three phases merge into exactly one Airtable row.** That is the whole outbound
design in one assertion, per workflow.

Same four safety properties as stage 3, and they are not flags:

1. **The production base is refused unconditionally.**
2. **Dry run is the default** — every payload printed, nothing sent.
3. **`--approved-by` is mandatory for `--live`** and echoed into the header.
4. **Every write upserts on a probe attempt id**, so a payload Airtable accepts
   when we expected refusal merges onto a probe row instead of littering the
   table. Rows are tagged `Operator Name = LABOS-PROBE`; LabOS never deletes, so
   ask the Airtable team to purge tagged rows.

What is deliberately NOT here: attachments. `LabOS Photos` has no upload path
yet, so there is nothing to send and pretending otherwise would be the exact
false confidence this file exists to remove.
"""

import argparse
import datetime as dt
import sys
import uuid

from app.airtable import contract as C
from app.airtable import envelope
from app.airtable.client import AirtableClient
from app.config import airtable_settings

BASE_TESTING = "app4oXS3Kd5IKWgJ7"
BASE_PRODUCTION = "app0OCunbmuXl7Hc9"
RAW = "tblnc9SsbXU0C0FWh"
MERGE_KEY = "LabOS Attempt ID"
PROBE_OPERATOR = "LABOS-PROBE"

STARTED = dt.datetime(2026, 9, 8, 9, 0, 0, tzinfo=dt.timezone.utc)
ENDED = dt.datetime(2026, 9, 8, 10, 30, 0, tzinfo=dt.timezone.utc)

# One row per workflow. `extra` carries only what that type's §5.1 matrix
# requires beyond the always-required set — which is why Impact has a value
# here and the two pass/fail types do not.
TYPES = (
    (C.STATIC_LOAD, {}),
    (C.CYCLES, {"Cycles Completed": 1000}),
    (C.IMPACT, {"Impact Result": "Pass - 3 of 3 impacts resisted"}),
    (C.FORCED_ENTRY, {}),
    (C.ANSI_Z97, {}),
)


def base_values(test_type, attempt_id, test_id):
    return {
        "Airtable Project ID": "recPROBEproject01",
        "Airtable Mock-Up ID": "recPROBEmockup001",
        "Airtable Protocol ID": "recPROBEprotocol1",
        "Airtable Section ID": "recPROBEsection01",
        "LabOS Test ID": test_id,
        "LabOS Attempt ID": attempt_id,
        "Attempt Number": 1,
        "Test Name": f"Stage 4 probe - {test_type}",
        "Test Type": test_type,
        "Test Result": C.RESULT_PENDING,
        "Operator Name": PROBE_OPERATOR,
        "Testing Start Date": STARTED,
        "LabOS Created At": STARTED,
        "LabOS Updated At": STARTED,
        "Schema Version": C.CONTRACT_VERSION,
        "Result Detail (JSON)": {"schema": C.CONTRACT_VERSION,
                                 "probe": True, "test_type": test_type},
    }


def phases_for(test_type, attempt_id, test_id, live_options):
    """The three payloads, built by the same builders `sync.publish` uses."""
    values = base_values(test_type, attempt_id, test_id)
    create = envelope.build_start(values, live_options=live_options)

    terminal_values = dict(values)
    terminal_values.update({
        "Testing End Date": ENDED,
        "Test Date": ENDED,
        "Testing Continued": "Stopped",
        "LabOS Updated At": ENDED,
        "Notes": "stage 4 probe",
    })
    for k, v in dict(TYPES).get(test_type, {}).items():
        terminal_values[k] = v
    terminal = envelope.build_terminal(terminal_values, live_options=live_options)

    verdict_values = dict(terminal_values)
    verdict_values.update({
        "Test Result": "Pass",
        "LabOS Verdict By": "LABOS-PROBE-reviewer",
        "LabOS Verdict At": ENDED,
        "Retest Required": False,
    })
    verdict = envelope.build_verdict(verdict_values, live_options=live_options)
    return (("create", create), ("terminal", terminal), ("verdict", verdict))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="actually write; without it nothing leaves the process")
    ap.add_argument("--approved-by", metavar="WHO",
                    help="required with --live; recorded in the header")
    args = ap.parse_args(argv)

    settings = airtable_settings
    if settings.base_id == BASE_PRODUCTION:
        print(f"REFUSED: this script never writes production ({BASE_PRODUCTION}).",
              file=sys.stderr)
        return 2
    if args.live:
        if not settings.token:
            print("REFUSED: no AIRTABLE_TOKEN set.", file=sys.stderr)
            return 2
        if not args.approved_by:
            print('REFUSED: --live needs --approved-by "<who, and when>".',
                  file=sys.stderr)
            return 2

    print("=" * 72)
    print("Stage 4 - five test types, three phases each")
    print("=" * 72)
    print(f"  base   : {settings.base_id or BASE_TESTING}")
    print(f"  table  : {RAW}")
    print(f"  mode   : {'LIVE - this will write' if args.live else 'DRY RUN'}")
    if args.live:
        print(f"  approved by: {args.approved_by}")
    print()

    client = AirtableClient()
    live_options = None
    if args.live:
        # Validate against the live option sets rather than a snapshot, so a
        # value the base no longer offers fails here and not mid-drain.
        tables = client.get_base_schema(settings.base_id or BASE_TESTING)["tables"]
        raw = next(t for t in tables if t["id"] == RAW)
        live_options = envelope.options_from_snapshot(
            {f["name"]: [c["name"] for c in (f.get("options") or {}).get("choices", [])]
             for f in raw["fields"]
             if (f.get("options") or {}).get("choices")})
        print("  validating against the LIVE option sets")
        print()

    failures = []
    for test_type, _ in TYPES:
        attempt_id = f"probe4-attempt-{uuid.uuid4()}"
        test_id = f"probe4-test-{uuid.uuid4()}"
        try:
            built = phases_for(test_type, attempt_id, test_id, live_options)
        except Exception as exc:                                  # noqa: BLE001
            failures.append(f"{test_type}: payload did not build - "
                            f"{type(exc).__name__}: {exc}")
            print(f"  [FAIL] {test_type:<14} {type(exc).__name__}: {exc}")
            continue

        if not args.live:
            print(f"  [BUILT] {test_type:<14} "
                  f"{', '.join(f'{p}={len(r)} fields' for p, r in built)}")
            continue

        ids = []
        try:
            for phase, record in built:
                resp = client.upsert_records(RAW, [record])
                got = [r["id"] for r in resp.get("records", [])]
                ids.append((phase, got))
        except Exception as exc:                                  # noqa: BLE001
            failures.append(f"{test_type}: {phase} rejected - "
                            f"{type(exc).__name__}: {exc}")
            print(f"  [FAIL] {test_type:<14} {phase}: {exc}")
            continue

        distinct = {i for _, got in ids for i in got}
        rows = client.list_records(
            RAW, formula=f"{{{MERGE_KEY}}}='{attempt_id}'").get("records", [])
        one_row = len(rows) == 1 and len(distinct) == 1
        if not one_row:
            failures.append(f"{test_type}: three phases produced "
                            f"{len(rows)} row(s) and {len(distinct)} record id(s)")
        # And the verdict actually landed on it, in their spelling.
        fields = rows[0]["fields"] if rows else {}
        verdict_ok = fields.get("Test Result") == "Passed"
        if not verdict_ok:
            failures.append(f"{test_type}: Test Result read back as "
                            f"{fields.get('Test Result')!r}, expected 'Passed'")
        mark = "PASS" if (one_row and verdict_ok) else "FAIL"
        print(f"  [{mark}] {test_type:<14} 3 phases -> {len(rows)} row "
              f"{sorted(distinct)} · Test Result={fields.get('Test Result')!r} "
              f"· Status={fields.get('Test Status')!r}")

    print()
    print("=" * 72)
    if failures:
        print(f"stage 4: {len(failures)} FAILURE(S)")
        for f in failures:
            print("  -", f)
        return 1
    if args.live:
        print(f"stage 4: all {len(TYPES)} test types published, "
              "three phases each, one row each")
        print(f"  rows tagged Operator Name = {PROBE_OPERATOR!r} - "
              "ask the Airtable team to purge them")
    else:
        print(f"stage 4: all {len(TYPES)} payload sets build. "
              "Add --live --approved-by to send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
