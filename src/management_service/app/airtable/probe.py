"""Airtable schema probe — contract v0.3 §10.1.

    python3 -m app.airtable.probe [--snapshot schema-snapshot.json] [--json]

One authenticated call to `GET /v0/meta/bases/{base}/tables` closes four open
items that would otherwise cost an email round-trip each:

    §10.1  is `Photos` an Attachment field, or URL/long text?
    §10.2  are the `Airtable … ID` fields plain text or link-to-record?
    §10.5  what is the `Impact Result` option set?
    §10.16 is the `Test Result` option `Pass` or `Passed`?

and verifies every table ID transcribed from their PDF into contract §0.1.

**This probe is strictly read-only.** It issues GETs and nothing else, so it is
safe to run against the production base — though there is no reason to until
cutover. It cannot settle §10.3 (are the read-side parameters machine-readable),
and says so: a long-text field named `Requirements` passes every schema check
and still fails the contract. That one needs a human answer.
"""

import argparse
import json
import sys

from ..config import TABLE_NAMES, TABLE_RAW_DATA, airtable_settings
from . import contract as C
from .client import AirtableClient
from .errors import AirtableError

PROTOCOL_SECTIONS = "tblqpvuJlSdkeS9PS"

TICK, CROSS, WARN, INFO = "PASS", "FAIL", "WARN", "  · "


class Report:
    """Collects findings so the exit code reflects what was actually found."""

    def __init__(self):
        self.lines = []
        self.failures = 0
        self.warnings = 0
        self.answers = {}

    def head(self, text):
        self.lines.append("")
        self.lines.append(text)
        self.lines.append("-" * len(text))

    def ok(self, text):
        self.lines.append(f"[{TICK}] {text}")

    def fail(self, text):
        self.failures += 1
        self.lines.append(f"[{CROSS}] {text}")

    def warn(self, text):
        self.warnings += 1
        self.lines.append(f"[{WARN}] {text}")

    def info(self, text):
        self.lines.append(f"{INFO}{text}")

    def answer(self, item, text):
        self.answers[item] = text

    def render(self):
        return "\n".join(self.lines)


def _field_index(table):
    return {f["name"]: f for f in table.get("fields", [])}


def _options(field):
    return [o["name"] for o in field.get("options", {}).get("choices", [])]


def check_tables(schema, rep):
    """Every table ID in contract §0.1 must exist, with the expected name."""
    rep.head("1. Table IDs (contract §0.1, transcribed from their PDF)")
    live = {t["id"]: t["name"] for t in schema.get("tables", [])}
    for table_id, expected_name in TABLE_NAMES.items():
        actual = live.get(table_id)
        if actual is None:
            # Only the five LabOS tables must exist in the testing base; the
            # operational four may legitimately live elsewhere.
            if table_id in (TABLE_RAW_DATA, PROTOCOL_SECTIONS):
                rep.fail(f"{table_id} ({expected_name}) NOT FOUND in this base")
            else:
                rep.info(f"{table_id} ({expected_name}) not in this base — expected for a testing base")
        elif actual != expected_name:
            rep.warn(f"{table_id} is named {actual!r}, contract §0.1 says {expected_name!r}")
        else:
            rep.ok(f"{table_id}  {actual}")
    extra = set(live) - set(TABLE_NAMES)
    for table_id in sorted(extra):
        rep.info(f"undocumented table in base: {table_id} ({live[table_id]})")
    return live


def check_raw_table(schema, rep):
    """Diff the write target against contract §4."""
    rep.head("2. LabOS Raw Data Table vs. contract §4")
    table = next((t for t in schema.get("tables", []) if t["id"] == TABLE_RAW_DATA), None)
    if table is None:
        rep.fail(f"write target {TABLE_RAW_DATA} not present — cannot diff the envelope")
        return
    fields = _field_index(table)

    missing = [n for n in C.EXPECTED_LIVE if n not in fields]
    for name in missing:
        rep.fail(f"promised by v2 but MISSING from the base: {name!r}")
    if not missing:
        rep.ok(f"all {len(C.EXPECTED_LIVE)} fields promised by v2 are present")

    still_absent = [n for n in C.BLOCKING_ABSENT if n not in fields]
    for name in still_absent:
        rep.warn(f"{name!r} still absent — contract §10.14, BLOCKING")
    for name in C.BLOCKING_ABSENT:
        if name in fields:
            rep.ok(f"{name!r} has been added — §10.14 can close")

    granted = [n for n in C.REQUESTED_ABSENT if n in fields]
    for name in sorted(granted):
        rep.ok(f"{name!r} added since v2 — §10.15 partially closes")

    undocumented = sorted(set(fields) - set(C.BY_WIRE_NAME))
    for name in undocumented:
        rep.info(f"field in base that the contract does not know: {name!r} ({fields[name]['type']})")

    # ---- type checks that would break the contract if violated
    rep.head("3. Type checks (contract §2, §7)")
    for name, forbidden in C.FORBIDDEN_TYPES.items():
        f = fields.get(name)
        if not f:
            continue
        if f["type"] in forbidden:
            rep.fail(f"{name!r} is type {f['type']!r} — forbidden ({forbidden})")
        else:
            rep.ok(f"{name!r} is {f['type']!r}")

    # ---- the four items the probe exists to answer
    rep.head("4. Open items this probe answers")

    photos = fields.get("Photos")
    if photos:
        verdict = "ATTACHMENT — violates §7" if photos["type"] == "multipleAttachments" else "not an attachment — matches §7"
        rep.answer("§10.1", f"Photos is {photos['type']!r} — {verdict}")
        (rep.fail if photos["type"] == "multipleAttachments" else rep.ok)(
            f"§10.1  Photos = {photos['type']!r}")
    else:
        rep.warn("§10.1  Photos field not present")

    id_fields = ["Airtable Project ID", "Airtable Mockup ID",
                 "Airtable Protocol ID", "Airtable Section ID"]
    types = {n: fields[n]["type"] for n in id_fields if n in fields}
    linked = [n for n, t in types.items() if t == "multipleRecordLinks"]
    if types:
        rep.answer("§10.2", f"ID field types: {types}")
        if linked:
            rep.warn(f"§10.2  link-to-record, not text: {linked} — LabOS proposed text")
        else:
            rep.ok(f"§10.2  all four ID fields are plain text ({sorted(set(types.values()))})")

    impact = fields.get("Impact Result")
    if impact:
        opts = _options(impact)
        rep.answer("§10.5", f"Impact Result is {impact['type']!r}, options={opts or 'free text'}")
        rep.ok(f"§10.5  Impact Result = {impact['type']!r}, options={opts or '(free text)'}")
    else:
        rep.warn("§10.5  Impact Result field not present")

    result = fields.get("Test Result")
    if result:
        opts = _options(result)
        expected = set(C.BY_LABOS_NAME["Test Result"].options)
        rep.answer("§10.16", f"Test Result options = {opts}")
        if opts and set(opts) != expected:
            rep.warn(f"§10.16  Test Result options are {opts}, contract §4.3 says {sorted(expected)}"
                     " — the client must send THEIR spelling")
        else:
            rep.ok(f"§10.16  Test Result options match the contract: {opts}")
    else:
        rep.warn("§10.16  Test Result field not present")

    # ---- every select option set, so the payload builder can be exact
    rep.head("5. Live select option sets (the client must send these verbatim)")
    for name in sorted(fields):
        f = fields[name]
        if f["type"] in ("singleSelect", "multipleSelects"):
            rep.info(f"{name}: {_options(f)}")


def check_read_side(schema, rep):
    """§10.3 — show the Protocol Sections shape; do not pretend to judge it."""
    rep.head("6. Protocol Sections — the read side (contract §9.1 / §10.3)")
    table = next((t for t in schema.get("tables", []) if t["id"] == PROTOCOL_SECTIONS), None)
    if table is None:
        rep.fail(f"Protocol Sections {PROTOCOL_SECTIONS} not present")
        return
    fields = _field_index(table)
    found = [n for n in C.READ_SIDE_EXPECTED if n in fields]
    for name in found:
        rep.ok(f"machine-readable parameter present: {name!r} ({fields[name]['type']})")
    if not found:
        rep.warn("none of the parameter fields contract §9.1 asks for exist under those names")
    rep.info("full field list, for judging §10.3 by hand:")
    for name in sorted(fields):
        rep.info(f"    {name}  —  {fields[name]['type']}")
    rep.info("")
    rep.info("A probe CANNOT close §10.3. A long-text field named 'Requirements' passes")
    rep.info("every schema check above and still fails the contract, because the operator")
    rep.info("would have to re-type the design pressures by hand. Read the types above and")
    rep.info("decide; that decision is the W3 gate.")


def build_snapshot(schema):
    """Field-ID snapshot for contract §9's deploy-time diff.

    Binding to `fld…` IDs makes an Airtable-side rename a non-event and turns a
    removal into a loud failure at deploy instead of a silent one at test time.
    """
    return {
        "contract_version": C.CONTRACT_VERSION,
        "base_id": airtable_settings.base_id,
        "tables": {
            t["id"]: {
                "name": t["name"],
                "fields": {f["name"]: {"id": f["id"], "type": f["type"],
                                       "options": _options(f) or None}
                           for f in t.get("fields", [])},
            }
            for t in schema.get("tables", [])
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Airtable schema probe (read-only)")
    parser.add_argument("--snapshot", metavar="PATH",
                        help="write the field-ID schema snapshot (contract §9)")
    parser.add_argument("--json", action="store_true",
                        help="emit the machine-readable answers instead of the report")
    args = parser.parse_args(argv)

    s = airtable_settings
    if not s.token:
        print("No AIRTABLE_TOKEN set.", file=sys.stderr)
        print("The Airtable team has not delivered the testing PAT yet "
              "(contract §10.10). Put it in .env — never in a commit, a "
              "document, or a browser-served config.", file=sys.stderr)
        return 2

    print(f"Airtable schema probe — base {s.base_id} ({s.environment}), read-only")
    print(f"contract v{C.CONTRACT_VERSION} · no write is issued by this command")

    client = AirtableClient(s)
    try:
        schema = client.get_base_schema()
    except AirtableError as exc:
        print(f"\nprobe failed: {exc}", file=sys.stderr)
        return 3

    rep = Report()
    live = check_tables(schema, rep)
    check_raw_table(schema, rep)
    check_read_side(schema, rep)

    if args.snapshot:
        with open(args.snapshot, "w", encoding="utf-8") as fh:
            json.dump(build_snapshot(schema), fh, indent=2, sort_keys=True)
        rep.head("7. Snapshot")
        rep.ok(f"field-ID snapshot written to {args.snapshot} — commit it (contract §9)")

    if args.json:
        print(json.dumps({"answers": rep.answers,
                          "failures": rep.failures,
                          "warnings": rep.warnings,
                          "tables": live}, indent=2, sort_keys=True))
    else:
        print(rep.render())
        print()
        print(f"summary: {rep.failures} failure(s), {rep.warnings} warning(s)")
        if rep.answers:
            print("\nanswers to carry back into contract §10:")
            for item in sorted(rep.answers):
                print(f"  {item}  {rep.answers[item]}")
        print("\n§10.3 (read-side parameter structure) is NOT answered here — see section 6.")

    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
