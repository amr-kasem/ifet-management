"""Generate the LabOS <-> Airtable interface schema as one CSV.

Deliverable 1 of the 2026-09-06 IFET request: "the structured schema for
fetching and posting data between LabOS and Airtable".

It is **generated, never hand-edited**, because the two inputs drift on
different clocks:

  * the live base schemas -- what Airtable actually holds today, read from the
    Meta API for both bases;
  * the field register -- what LabOS has decided to read, write, omit or
    propose (`ifet-firmware/docs/labos-airtable/contract/field-register.csv`).

The output is the union, one row per field per table, so a reader can answer
three different questions from one file: what exists, what LabOS touches, and
what is still only proposed. Fields Airtable holds that LabOS deliberately
ignores are included with `labos_use=ignore` -- that is the machine-readable
form of "LabOS does not need billing, pricing, invoices or scheduling data".

Read-only. It never writes to Airtable.

    python -m app.airtable.interface_schema --out interface-schema.csv
"""

import argparse
import csv
import json
import os
import re
import urllib.request

BASES = {"testing": "app4oXS3Kd5IKWgJ7", "production": "app0OCunbmuXl7Hc9"}
TOKEN_ENV = {"testing": "AIRTABLE_TOKEN", "production": "AIRTABLE_TOKEN_PRODUCTION"}

COLUMNS = [
    "airtable_table", "airtable_table_id", "airtable_field",
    "field_id_testing", "field_id_production", "in_testing", "in_production",
    "airtable_type", "choices",
    "labos_use", "direction", "labos_source", "write_phase",
    "status", "delivery_state", "owner", "rule",
]

# Register rows that name API metadata or local-only concepts rather than a
# field anyone can create. They stay in the output, flagged, so nobody tries.
NOT_A_FIELD = ("record_id", "(delivery metadata)")


def load_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as fh:
        for line in fh:
            m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def fetch_tables(base_id, token):
    req = urllib.request.Request(
        f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["tables"]


def choices_of(field):
    opts = (field.get("options") or {}).get("choices") or []
    return " | ".join(c.get("name", "") for c in opts)


def build(live, register):
    """live: {base: [tables]}; register: list of register rows."""
    # index the live schemas
    index = {}          # (table_name, field_name) -> {base: field}
    tables = {}         # table_name -> table_id
    for base, tbls in live.items():
        for t in tbls:
            tables.setdefault(t["name"], t["id"])
            for f in t["fields"]:
                index.setdefault((t["name"], f["name"]), {})[base] = f

    reg = {(r["airtable_table"], r["airtable_field"]): r for r in register}

    rows = []
    for key in sorted(set(index) | set(reg)):
        table, field = key
        seen = index.get(key, {})
        r = reg.get(key)
        t_f, p_f = seen.get("testing"), seen.get("production")
        any_f = t_f or p_f

        if r is None:
            use, direction = "ignore", ""
        elif field in NOT_A_FIELD or r["airtable_field"].startswith("("):
            use, direction = "not-a-field", r["direction"]
        elif r["direction"] == "OUT":
            use, direction = "write", "OUT"
        elif r["direction"] == "IN":
            use, direction = "read", "IN"
        elif r["direction"] == "IGNORED":
            # The field exists in the base and we deliberately do not read it.
            # Distinct from a field absent from the register, which is ignored
            # because nobody ever considered it: an IGNORED row keeps the reason
            # in `rule`. Both surface as labos_use=ignore, because that is the
            # truth about the interface.
            use, direction = "ignore", "IGNORED"
        else:
            use, direction = "read-only", r["direction"]

        rows.append({
            "airtable_table": table,
            "airtable_table_id": tables.get(table, ""),
            "airtable_field": field,
            "field_id_testing": (t_f or {}).get("id", ""),
            "field_id_production": (p_f or {}).get("id", ""),
            "in_testing": "yes" if t_f else "no",
            "in_production": "yes" if p_f else "no",
            "airtable_type": (any_f or {}).get("type") or (r or {}).get("airtable_type", ""),
            "choices": choices_of(any_f) if any_f else "",
            "labos_use": use,
            "direction": direction,
            "labos_source": (r or {}).get("labos_source", ""),
            "write_phase": (r or {}).get("write_phase", ""),
            "status": (r or {}).get("status", ""),
            "delivery_state": (r or {}).get("delivery_state", ""),
            "owner": (r or {}).get("owner", ""),
            "rule": (r or {}).get("rule", ""),
        })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="CSV to write")
    ap.add_argument("--register", required=True, help="field-register.csv")
    ap.add_argument("--env", default=".env", help="file holding the Airtable PATs")
    args = ap.parse_args(argv)

    env = {**load_env(args.env), **os.environ}
    live = {}
    for name, base_id in BASES.items():
        token = env.get(TOKEN_ENV[name])
        if not token:
            raise SystemExit(f"no token in {TOKEN_ENV[name]} for base {name}")
        live[name] = fetch_tables(base_id, token)

    with open(args.register) as fh:
        register = list(csv.DictReader(fh))

    rows = build(live, register)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    real = [r for r in rows if r["labos_use"] != "not-a-field"]
    used = [r for r in real if r["labos_use"] != "ignore"]
    absent = [r for r in real if r["in_testing"] == "no"]
    planned = sum(1 for r in absent if r["delivery_state"] == "PLANNED")
    print(f"{len(rows)} rows -> {args.out}")
    print(f"  {len(real)} real fields: {len(used)} LabOS touches, "
          f"{len(real) - len(used)} deliberately ignored")
    print(f"  {len(absent)} absent from the testing base: "
          f"{planned} decided, {len(absent) - planned} only proposed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
