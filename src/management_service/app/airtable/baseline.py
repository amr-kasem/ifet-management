"""Baseline export — schema + records, both bases, strictly read-only.

    python3 -m app.airtable.baseline --base both --schema-out DIR --records-out DIR

This is step 1 of the Testing Base change programme: capture what exists
*before* we add a single field, so every later claim about what changed can be
diffed against an artifact rather than against memory.

Two outputs, deliberately separated, because they have different disclosure
rules:

* **Schema** — table/field IDs, names, types, select options. No customer data.
  Safe to commit, and it must be: the field-ID maps are per base, they cannot be
  hand-typed, and the change register diffs against them.
* **Records** — the actual rows. `IFET Projects` alone carries customer names,
  contact emails, proposal amounts, balances and QuickBooks invoice IDs, and
  both LabOS repos are public on GitHub. These go outside git. The exporter
  refuses to write them into a work tree unless told twice.

Read-only in the same sense as `probe`: GETs only, no write path imported, so
running it against production is safe.
"""

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys

from ..config import (BASE_PRODUCTION, BASE_TESTING, TABLE_NAMES,
                      AirtableSettings)
from .client import AirtableClient
from .errors import AirtableError

BASES = {
    "testing": (BASE_TESTING, "AIRTABLE_TOKEN"),
    "production": (BASE_PRODUCTION, "AIRTABLE_TOKEN_PRODUCTION"),
}


def _client(base_name, env=None):
    """A read-only client bound to one base.

    Built from an explicit env dict rather than the process settings because the
    two bases use different tokens, and because a baseline run must never
    inherit a write allowlist from whatever the container happens to be
    configured for.
    """
    env = os.environ if env is None else env
    base_id, token_var = BASES[base_name]
    token = env.get(token_var, "").strip()
    if not token:
        raise SystemExit(f"{token_var} is not set — cannot read {base_name}")
    settings = AirtableSettings(env={
        "AIRTABLE_TOKEN": token,
        "AIRTABLE_BASE_ID": base_id,
        # Empty allowlist: nothing in this module writes, and if that ever
        # stops being true the client raises rather than succeeding.
        "AIRTABLE_WRITE_ALLOWLIST": "",
        "AIRTABLE_ALLOW_PRODUCTION_WRITE": "false",
    })
    return settings, AirtableClient(settings=settings)


def fetch_schema(client, base_id):
    """Raw Meta API response, unmodified.

    Stored verbatim on purpose. A normalised view is easy to regenerate; the
    field property we did not think to keep is not.
    """
    return client.get_base_schema(base_id)


def flatten_fields(schema):
    """One row per field, for the change register to diff against."""
    rows = []
    for table in schema.get("tables", []):
        for position, field in enumerate(table.get("fields", [])):
            options = field.get("options") or {}
            choices = options.get("choices")
            rows.append({
                "table_id": table.get("id", ""),
                "table_name": table.get("name", ""),
                "field_position": position,
                "field_id": field.get("id", ""),
                "field_name": field.get("name", ""),
                "field_type": field.get("type", ""),
                "description": (field.get("description") or "").replace("\n", " "),
                # Select options are the half of the schema that changes most
                # often and breaks writes hardest — Test Type is the live
                # example. Keep them in the flat view, not just the JSON.
                "choices": " | ".join(c.get("name", "") for c in choices) if choices else "",
                "linked_table_id": options.get("linkedTableId", ""),
                "options_json": json.dumps(options, sort_keys=True) if options else "",
            })
    return rows


def _cell(value):
    """Airtable values into one CSV cell, losslessly enough to diff.

    Scalars pass through; anything structured (linked record ID arrays, lookups,
    attachment objects) becomes compact JSON rather than Python's repr, so the
    CSV stays machine-readable.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def export_records(client, table, out_dir):
    """One CSV per table. Returns (path, row_count).

    The header comes from the schema, not from the records: Airtable omits empty
    fields from a record payload, so a header inferred from rows would vary with
    the data and make two exports undiffable.
    """
    table_id = table.get("id", "")
    name = table.get("name", table_id)
    field_names = [f.get("name", "") for f in table.get("fields", [])]
    header = ["record_id", "createdTime"] + field_names

    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name).strip("-")
    path = os.path.join(out_dir, f"{safe}__{table_id}.csv")

    count = 0
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        # max_pages guards a runaway loop, not the data: 1000 pages is 100k
        # records, well past anything in these bases.
        for record in client.iter_records(table_id, max_pages=1000):
            fields = record.get("fields", {})
            writer.writerow(
                [record.get("id", ""), record.get("createdTime", "")]
                + [_cell(fields.get(fname)) for fname in field_names]
            )
            count += 1
    return path, count


def _in_git_worktree(path):
    """True if `path` sits inside a git work tree. Both LabOS repos are public."""
    target = os.path.abspath(path)
    probe = target if os.path.isdir(target) else os.path.dirname(target)
    try:
        result = subprocess.run(
            ["git", "-C", probe, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def run(base_name, schema_out, records_out, want_records, allow_git_records):
    settings, client = _client(base_name)
    base_id = settings.base_id
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    log = []

    os.makedirs(schema_out, exist_ok=True)
    schema = fetch_schema(client, base_id)

    raw_path = os.path.join(schema_out, f"{base_name}-{base_id}-schema-{stamp}.json")
    with open(raw_path, "w", encoding="utf-8") as handle:
        json.dump(schema, handle, indent=2, sort_keys=True)
    log.append(f"schema  {raw_path}")

    rows = flatten_fields(schema)
    flat_path = os.path.join(schema_out, f"{base_name}-{base_id}-fields-{stamp}.csv")
    with open(flat_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["table_id"])
        writer.writeheader()
        writer.writerows(rows)
    log.append(f"fields  {flat_path}  ({len(rows)} fields, "
               f"{len(schema.get('tables', []))} tables)")

    if want_records:
        toplevel = _in_git_worktree(records_out)
        if toplevel and not allow_git_records:
            raise SystemExit(
                f"refusing to write records into a git work tree ({toplevel}).\n"
                "These rows carry customer names, emails and invoice amounts, and\n"
                "both LabOS repos are public. Choose a path outside git, or pass\n"
                "--i-know-records-are-confidential to override."
            )
        os.makedirs(records_out, exist_ok=True)
        for table in schema.get("tables", []):
            if table.get("id") not in TABLE_NAMES:
                log.append(f"        ! {table.get('name')} ({table.get('id')}) "
                           f"is not in config.TABLE_NAMES")
            path, count = export_records(client, table, records_out)
            log.append(f"records {path}  ({count} rows)")

    return log


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Airtable baseline export (read-only): schema + records.")
    parser.add_argument("--base", choices=["testing", "production", "both"],
                        default="both")
    parser.add_argument("--schema-out", required=True,
                        help="directory for schema JSON + field CSV (safe to commit)")
    parser.add_argument("--records-out", default=None,
                        help="directory for record CSVs (must be outside git)")
    parser.add_argument("--no-records", action="store_true",
                        help="schema only")
    parser.add_argument("--i-know-records-are-confidential", action="store_true",
                        dest="allow_git_records",
                        help="permit record CSVs inside a git work tree")
    args = parser.parse_args(argv)

    want_records = not args.no_records
    if want_records and not args.records_out:
        parser.error("--records-out is required unless --no-records")

    names = ["testing", "production"] if args.base == "both" else [args.base]
    failures = 0
    for name in names:
        print(f"\n=== {name} ===")
        try:
            for line in run(name, os.path.join(args.schema_out, name),
                            os.path.join(args.records_out, name) if args.records_out else None,
                            want_records, args.allow_git_records):
                print(line)
        except AirtableError as exc:
            failures += 1
            print(f"FAILED: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
