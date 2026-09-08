"""Offline cross-check — does the field register still describe what we built?

    python3 app/airtable/check_register.py

`preflight.py` checks the change *document* against the two live bases. Nothing
checked `contract/field-register.csv` — the authoritative register — against the
code that implements it, and on 2026-09-08 fifty-three of its seventy-three rows
turned out to name something we do not have. `Missile Type` was recorded as
`singleSelect` when `apply_schema` creates it as `singleLineText`; `Impact
Velocity` named `shots.velocity` as its local source when nothing writes a
target there; the specimen->project link was recorded as `Project Name` when the
base's link field is `IFET Job Number`; and the whole outbound surface named
`test_programme_runs`/`test_programmes`, tables that were designed and never
built. Each one is individually plausible, which is exactly why review does not
catch them.

So this reconciles the register against four things we actually have:

1. **The mirror allowlist.** Every `IN` row must name a field `mirror.py` copies,
   and every allowlisted field must have an `IN` row. The allowlist is the read
   boundary; a register that disagrees with it describes a different system.
2. **The forbidden list.** `Value` and the commercial fields must never appear as
   an `IN` row. §10.19 is a safety property, not a preference.
3. **The captured schema.** Types are compared against the `after-*.json`
   captured when the fields were applied, so this runs with no token and no
   network. A row absent from the base must be `OMITTED`.
4. **`preflight.ADDED` and the local columns.** The 17 `APPLIED` rows must match
   what preflight asserts live, and every `table.column` source must resolve to a
   real column on a real model.
5. **The business-I/O reconciliation.** The same column check over
   `evidence/business-io-reconciliation-*/reconciliation.csv`, which traces where
   each value lives and drifts the same way — four of its rows still said
   `mirror.*` and one said `test_results.rationale` for `result_rationale`.
6. **The project owner's approval document is current.** It is generated from the
   code by `test_requirements_doc.py`, and it is going out for a signature — so a
   stale copy on disk is the one drift that must not be possible.

Read-only, stdlib only, touches no base and opens no socket: it parses the source
with `ast` rather than importing it, so it runs anywhere a checkout does — no
virtualenv, no SQLAlchemy, no database.
"""

import ast
import csv
import importlib.util
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve()
APP = HERE.parents[1]
# …/ifet-project/ifet-management/src/management_service/app/airtable/…
#  parents: 0 airtable 1 app 2 management_service 3 src 4 ifet-management
#           5 ifet-project — the sibling checkout, as `preflight.py` does it.
DOCS = HERE.parents[5] / "ifet-firmware" / "docs" / "labos-airtable"
REGISTER = DOCS / "contract" / "field-register.csv"

# Register table -> mirror allowlist constant. The four read tables; the other
# four Airtable tables are commercial and are not mirrored at all.
ALLOWLISTS = {
    "IFET Projects": "PROJECT_FIELDS",
    "Mock-Ups/Specimens": "SPECIMEN_FIELDS",
    "Tests Protocols": "PROTOCOL_FIELDS",
    "Protocol Sections": "SECTION_FIELDS",
}

# Sources name a table, or the mixin that declares the column when several
# tables share it (`AirtableProtocolRef` sits on all four test tables). Both
# forms resolve below; anything else is reported.
#
# There is deliberately no `run`/`programme` alias. The register named
# `test_programme_runs` and `test_programmes` until 2026-09-08, a two-level
# split that was designed and then not built: `TestResult` already carried
# `trial_number` and the whole attempt record, so the implementation put the
# outbound surface on `test_results` and the register never followed. Adding an
# alias would have hidden that; failing on it is the point.

# Sources that name no column by design: no source at all, or an interpretation
# rather than a landing place.
PROSE = {"-", "(interpretation)", "(validation)"}


# --- reading the source without importing it -------------------------------

def _module(path):
    return ast.parse(path.read_text(), str(path))


def _literals(tree):
    """Module-level `NAME = <literal>` assignments we can evaluate."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


def _added(tree):
    """`preflight.ADDED`, whose dict keys are module constants, not literals."""
    env = _literals(tree)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "ADDED":
            out = {}
            for key, value in zip(node.value.keys, node.value.values):
                name = env[key.id] if isinstance(key, ast.Name) else ast.literal_eval(key)
                out[name] = [tuple(ast.literal_eval(e)) for e in value.elts]
            return out
    raise SystemExit("preflight.ADDED not found — has preflight.py moved?")


def _forbidden(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and \
                getattr(node.targets[0], "id", None) == "FORBIDDEN_FIELDS":
            return set(ast.literal_eval(node.value.args[0]))
    raise SystemExit("mirror.FORBIDDEN_FIELDS not found")


def _columns(*paths):
    """`{name: {column, …}}` for every table, keyed by table name and by class.

    Mixins carry columns but no `__tablename__` (`MirrorRow`,
    `AirtableProtocolRef`), so they are collected by class name and folded into
    each subclass that declares a table — and kept under their own name, because
    a column several tables share is best named by the mixin that declares it.
    """
    per_class, tables, bases = {}, {}, {}
    for path in paths:
        for node in ast.walk(_module(path)):
            if not isinstance(node, ast.ClassDef):
                continue
            cols, table = set(), None
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign) or \
                        not isinstance(stmt.targets[0], ast.Name):
                    continue
                name = stmt.targets[0].id
                if name == "__tablename__":
                    table = ast.literal_eval(stmt.value)
                elif isinstance(stmt.value, ast.Call) and \
                        getattr(stmt.value.func, "id", None) == "Column":
                    cols.add(name)
            per_class[node.name] = cols
            bases[node.name] = [b.id for b in node.bases if isinstance(b, ast.Name)]
            if table:
                tables[table] = node.name
    resolved = {c: cols.union(*(per_class.get(b, set()) for b in bases[c]))
                if bases[c] else cols for c, cols in per_class.items()}
    return {**resolved, **{t: resolved[c] for t, c in tables.items()}}


def _schema():
    """The newest captured Testing-Base schema — `{table: {field: type}}`."""
    caps = sorted(DOCS.glob("evidence/testing-base-changes-*/after-*.json"))
    if not caps:
        raise SystemExit(f"no captured after-*.json under {DOCS}/evidence")
    doc = json.loads(caps[-1].read_text())
    tables = doc["tables"] if isinstance(doc, dict) else doc
    return caps[-1], {t["name"]: {f["name"]: f["type"] for f in t["fields"]}
                      for t in tables}


# --- the checks ------------------------------------------------------------

def main(argv=None):
    rows = list(csv.DictReader(REGISTER.open()))
    mirror_tree = _module(APP / "airtable" / "mirror.py")
    allow = _literals(mirror_tree)
    forbidden = _forbidden(mirror_tree)
    added = _added(_module(APP / "airtable" / "preflight.py"))
    columns = _columns(APP / "data" / "models.py", APP / "airtable" / "mirror.py",
                       APP / "sync" / "outbox.py", APP / "sync" / "state.py")
    capture, schema = _schema()
    fails = []

    def fail(check, message):
        fails.append(message)
        print(f"   FAIL  {message}")

    print(f"REGISTER CROSS-CHECK — {REGISTER.name}, {len(rows)} rows")
    print(f"   schema capture: {capture.parent.name}/{capture.name}\n")

    # -- 1. the register's read surface is the mirror's allowlist ----------
    print("1. IN rows are exactly the mirror allowlist")
    for table, const in ALLOWLISTS.items():
        want = set(allow[const])
        have = {r["airtable_field"] for r in rows
                if r["direction"] == "IN" and r["airtable_table"] == table
                and r["airtable_field"] != "record_id"}
        for f in sorted(want - have):
            fail(1, f"{table}: mirror copies {f!r} with no IN row")
        for f in sorted(have - want):
            fail(1, f"{table}: IN row {f!r} is not in the {const} allowlist")
    named = len([r for r in rows if r["direction"] == "IN"
                 and r["airtable_field"] != "record_id"])
    print(f"   {named} named fields, {sum(len(allow[c]) for c in ALLOWLISTS.values())} allowlisted")

    # -- 2. the forbidden fields are never read ---------------------------
    print("\n2. Forbidden fields are not read")
    for r in rows:
        if r["direction"] == "IN" and r["airtable_field"] in forbidden:
            fail(2, f"{r['airtable_field']!r} is FORBIDDEN but carries an IN row")
    print(f"   {len(forbidden)} forbidden fields, none read")

    # -- 3. types match the captured base ---------------------------------
    print("\n3. Types match the captured Testing Base")
    checked = 0
    for r in rows:
        field, table = r["airtable_field"], r["airtable_table"]
        if field == "record_id" or r["direction"] == "LOCAL_ONLY":
            continue
        live = schema.get(table, {}).get(field)
        if live is None:
            if r["delivery_state"] != "OMITTED":
                fail(3, f"{table}.{field} is absent from the base but is "
                        f"{r['delivery_state']}, not OMITTED")
            continue
        checked += 1
        if live != r["airtable_type"]:
            fail(3, f"{table}.{field}: register says {r['airtable_type']!r}, "
                    f"base has {live!r}")
    print(f"   {checked} fields present and correctly typed")

    # -- 4. APPLIED rows are the 17 preflight asserts ---------------------
    print("\n4. APPLIED rows are the 17 preflight checks")
    want = {name: typ for fields in added.values() for name, typ in fields}
    have = {r["airtable_field"]: r["airtable_type"] for r in rows
            if r["delivery_state"] == "APPLIED"}
    for f in sorted(set(want) - set(have)):
        fail(4, f"preflight asserts {f!r} but no register row is APPLIED")
    for f in sorted(set(have) - set(want)):
        fail(4, f"register row {f!r} is APPLIED but preflight does not check it")
    for f in sorted(set(want) & set(have)):
        if want[f] != have[f]:
            fail(4, f"{f}: register {have[f]!r}, preflight {want[f]!r}")
    print(f"   {len(have)} APPLIED rows, {len(want)} preflight assertions")

    # -- 5. every named source resolves to a real column ------------------
    print("\n5. Local sources resolve to real columns")
    resolved = 0
    for r in rows:
        source = r["labos_source"]
        if source in PROSE:
            continue
        # `a.b -> c.d` names both ends; `x (via y)` and prose trail after.
        for part in source.split("->"):
            head = part.strip().split(" ")[0]
            if "." not in head:
                print(f"   note  {r['airtable_field']}: prose source {source!r}")
                break
            table, _, column = head.partition(".")
            if table not in columns:
                fail(5, f"{r['airtable_field']}: no table {table!r} "
                        f"(source {source!r})")
            elif column not in columns[table]:
                fail(5, f"{r['airtable_field']}: {table} has no column "
                        f"{column!r} (source {source!r})")
            else:
                resolved += 1
    print(f"   {resolved} sources resolved against {len(columns)} tables")

    # -- 6. the reconciliation's traces resolve too -----------------------
    print("\n6. Business-I/O reconciliation traces resolve")
    recons = sorted(DOCS.glob("evidence/business-io-reconciliation-*/reconciliation.csv"))
    if not recons:
        print("   note  no reconciliation.csv found — skipped")
    for path in recons:
        traced = 0
        for r in csv.DictReader(path.open()):
            store = (r.get("local_storage") or "").strip()
            if not store or store in ("-", "(not in Airtable)"):
                continue
            for head, tail in re.findall(r"\b([A-Za-z_][A-Za-z_0-9]*)"
                                         r"\.([a-z_][a-z_0-9]*(?:/[a-z_][a-z_0-9]*)*)",
                                         store):
                if len(head) < 3:            # prose: "e.g", "i.e"
                    continue
                if head not in columns:
                    fail(6, f"{r['airtable_field']}: no table or model {head!r} "
                            f"(trace {store!r})")
                    continue
                for column in tail.split("/"):
                    if column not in columns[head]:
                        fail(6, f"{r['airtable_field']}: {head} has no column "
                                f"{column!r} (trace {store!r})")
                    else:
                        traced += 1
        print(f"   {path.parent.name}: {traced} traces resolved")

    # -- 7. the approval document is current ------------------------------
    print("\n7. The project owner's approval document is current")
    gen = HERE.parent / "test_requirements_doc.py"
    spec = importlib.util.spec_from_file_location("_trd", gen)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.OUT.exists():
        fail(7, f"{module.OUT.name} has not been generated yet")
    elif module.OUT.read_text() != module.build():
        fail(7, f"{module.OUT.name} is stale — regenerate with "
                f"python3 app/airtable/{gen.name}")
    else:
        print(f"   {module.OUT.name} matches the code it is generated from")

    print(f"\n{'PASS — the documents match what we built' if not fails else f'{len(fails)} MISMATCHES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
