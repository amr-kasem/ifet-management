"""Generate the five-test requirements document the project owner approves.

    python3 app/airtable/test_requirements_doc.py            # write it
    python3 app/airtable/test_requirements_doc.py --check     # is it current?

**Why this is generated and not written.** We already maintain three views of
the same interface by hand — the prose contract, `field-register.csv` (73 rows)
and `reconciliation.csv` (64 rows) — and on 2026-09-08 fifty-three of the
register's rows turned out to name something we do not have. A fourth
hand-authored view, this one going out for a signature, is the worst of them to
let drift: an approval against stale facts is worse than no approval.

So every number and every field name below is read out of the thing that
decides it:

- the stage tables from `domain/*_pressure_calculator.py` — the factors and
  cycle counts LabOS actually multiplies, not a remembered copy of them;
- which requirement code becomes which test from `importer.LOCAL_TYPE_BY_CODE`,
  and which codes execute at all from `requirements.EXECUTABLE_CODES`;
- the kind and unit each code must carry from `requirements.KIND_BY_CODE` and
  `UNITS_BY_KIND`;
- the five `Test Type` options from `contract.py`;
- the per-test field rows from `reconciliation.csv`, which is already keyed by
  test type, with types and write phases from `field-register.csv`.

Only the narrative is hand-written, and it is held in `PROSE` below so it sits
next to the code it describes. Read-only and stdlib-only: it parses the source
with `ast` rather than importing it, so it runs in a bare checkout.

`--check` regenerates into memory and compares, so `check_register.py` can fail
when the document and the code have parted company.
"""

import argparse
import ast
import csv
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve()
APP = HERE.parents[1]
DOCS = HERE.parents[5] / "ifet-firmware" / "docs" / "labos-airtable"
OUT = DOCS / "correspondence" / "five-test-requirements-approval-2026-09-08.md"
REGISTER = DOCS / "contract" / "field-register.csv"


def _literals(path):
    """Module-level `NAME = <literal>` assignments, evaluated."""
    out = {}
    for node in ast.parse(path.read_text()).body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and
                isinstance(node.targets[0], ast.Name)):
            continue
        value = node.value
        # `frozenset({...})` is a call, not a literal, and two of the sets this
        # document depends on are written that way.
        if isinstance(value, ast.Call) and \
                getattr(value.func, "id", None) in ("frozenset", "set") and value.args:
            value = value.args[0]
        try:
            out[node.targets[0].id] = ast.literal_eval(value)
        except ValueError:
            pass
    return out


def _class_literals(path):
    """`ClassName.ATTR = <literal>` for class-level constants."""
    out = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name):
                try:
                    out[stmt.targets[0].id] = ast.literal_eval(stmt.value)
                except ValueError:
                    pass
    return out


def _static_hold_seconds():
    """The dwell `get_static_test_data` returns beside the pressure."""
    tree = ast.parse((APP / "domain" / "static_test_pressure_calculator.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_static_test_data":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Tuple):
                    return ast.literal_eval(stmt.value.elts[1])
    raise SystemExit("static hold time not found — has the calculator changed?")


def _test_type_options():
    """The five `Test Type` options, from the contract module."""
    src = (APP / "airtable" / "contract.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.keyword) and node.arg == "options":
            try:
                options = ast.literal_eval(node.value)
            except ValueError:
                continue
            if "Static Load" in options:
                return options
    raise SystemExit("the five Test Type options were not found in contract.py")


def _rows(path, key):
    rows = list(csv.DictReader(path.open()))
    grouped = {}
    for r in rows:
        grouped.setdefault(r[key], []).append(r)
    return rows, grouped


# --- the narrative, the one part that cannot be derived --------------------
#
# Keyed by the `test_type` column of the reconciliation, so a test with no
# prose here is a visible hole rather than a silently plain page.

PROSE = {
    "STATIC": {
        "title": "Static Load",
        "code": "STATIC_PRESSURE",
        "what": "Hold a pressure against the specimen and measure how far it deflects, "
                "in both directions, at three multiples of the design pressure.",
        "derived": "**You supply one pair of numbers and LabOS produces all six stages.** "
                   "The proposal's inward and outward design pressures are the only "
                   "input; every stage is that pair times a fixed factor, alternating "
                   "direction. Nothing about the sequence comes from Airtable, which is "
                   "the single biggest difference from the flow as it was described to us.",
        "cannot": "**Deflection readings are not published.** The rigs return raw counts "
                  "from the gauges that were never calibrated to a physical unit, so a "
                  "number in a field named for inches would be a number we cannot stand "
                  "behind. It needs bench time, and it is hardware work.\n\n"
                  "**`Max Pressure Achieved` is not published either, for a different "
                  "reason.** The actual pressure exists on the rig's telemetry and renders "
                  "live in our UI; nothing subscribes to it and stores the maximum. That "
                  "is software work on our side and it is scheduled. We would rather send "
                  "nothing than send a number we cannot stand behind — but the two are not "
                  "the same admission.\n\n"
                  "**`recovery` is a configured constant, not a measurement.** It is the "
                  "settling time the rig waits, taken from config; it is not the specimen's "
                  "observed recovery.",
    },
    "CYCLIC": {
        "title": "Cycles",
        "code": "CYCLIC_PRESSURE",
        "what": "Cycle the pressure between a low and a high value for a fixed number of "
                "cycles, eight stages, four inward then four outward.",
        "derived": "**The same one pair of numbers produces all eight stages** — the high "
                   "and low pressure of each, and how many cycles it runs. Together with "
                   "Static Load that is fourteen stages from two numbers.\n\n"
                   "If a protocol's Static and Cycles sections ever disagree about the "
                   "design pressures, LabOS refuses rather than picking one: all fourteen "
                   "stages come from a single pair, so two pairs cannot both hold.",
        "cannot": "`Cycles Required` and `Cycles Completed` have no columns of their own in "
                  "Airtable and travel in the JSON response instead. Cycles completed is "
                  "captured at the moment the attempt terminates, because a reset or a "
                  "second run overwrites the live counter.",
    },
    "IMPACT": {
        "title": "Impact",
        "code": "IMPACT_LMI · IMPACT_SMI",
        "what": "Fire a missile at the specimen a required number of times and record, for "
                "each impact, whether it passed.",
        "derived": "**One test type, not two.** Large missile and small missile are the same "
                   "procedure with a different missile, so the missile is a field on the "
                   "test and not a separate kind of test.\n\n"
                   "**The impact classification is chosen in LabOS, not supplied by "
                   "Airtable — your decision of 2026-09-10.** One value covers the "
                   "missile, its weight and the target velocity: *SMI*, *LMI Level D* or "
                   "*LMI Level E*. Airtable's requirement code already says whether a "
                   "section is large missile or small, so that half is filled in for you "
                   "and cannot be contradicted; the only thing anyone chooses is D or E, "
                   "and only on a large-missile test. The classification is then sent "
                   "back to Airtable with the result.\n\n"
                   "**The target velocity is entered in LabOS too**, and it is not worked "
                   "out from the classification: we hold no table that says which "
                   "velocity each one means, and inventing one would put a number in "
                   "every impact record that nobody had checked.\n\n"
                   "**One attempt per impact — as you specified on 2026-09-08.** An impact "
                   "test contains one or more attempts and each attempt is exactly one "
                   "impact, with its own pass/fail, its own photographs and its own "
                   "verdict. Impact 1, 2, 3 are three attempts, not one attempt holding "
                   "three impacts, and none of them ever overwrites another.\n\n"
                   "There is no separate notion of re-doing impact 3: a specimen already "
                   "struck cannot have that impact repeated, so a further firing is impact "
                   "6, which is simply the next attempt. A result recorded *wrongly* is a "
                   "different thing and is superseded rather than overwritten — see the "
                   "last page.",
        "cannot": "**The new shape is built, and not yet deployed.** Everything above is "
                  "your 2026-09-08 instruction, implemented the same day: an attempt "
                  "refuses a second impact, a completed attempt must have exactly one, and "
                  "the attempt's own outcome is taken from its impact. `Impact Number` was "
                  "added to the Airtable schema and is published for Impact only, so a "
                  "roll-up on their side can count *tests* and *impacts* separately — "
                  "without it a five-impact test would read as five tests, and read "
                  "plausibly.\n\n"
                  "**What is not done is the deployment.** The database migration that "
                  "splits the impacts already recorded — 39 tests and 114 impacts on the "
                  "live system — is written and rehearsed forward and back against a copy, "
                  "and has not been run on the real database. Nothing in this integration "
                  "is deployed yet.\n\n"
                  "**Five impacts become five rows in the Airtable base**, each with its "
                  "own verdict and photographs, where today they are one row with a "
                  "summary line. This follows directly from attempts being the unit we "
                  "publish.\n\n"
                  "**The target impact velocity is read but never shown.** It comes across "
                  "from the proposal and is frozen onto the attempt, so it is in the "
                  "record — but it does not appear on the test the operator is looking at. "
                  "Whether it should is question 3.\n\n"
                  "**Impact location has no Airtable field, deliberately.** LabOS records "
                  "where each impact landed; we did not create a field for it on their "
                  "side, because location is an observation per impact and not a "
                  "requirement.",
    },
    "FORCED_ENTRY": {
        "title": "Forced Entry",
        "code": "FORCED_ENTRY",
        "what": "Attempt entry against the specimen and record pass or fail against a "
                "named grade, with notes and photographs.",
        "derived": "**No numeric requirement at all.** The proposal supplies the grade — "
                   "*ASTM F588 Grade 40* — and nothing else; the verdict is the operator's. "
                   "A grade LabOS does not recognise is displayed and the test stays "
                   "non-executable rather than being guessed at.\n\n"
                   "This test does not touch the rig hardware.",
        "cannot": "**There is no dedicated `Forced Entry Result` column in Airtable.** The "
                  "verdict lands in the shared `Test Result`, with detail in `Notes` and "
                  "the JSON. `Test Type` and `Test Result` are both single-selects, so "
                  "their views still filter and group both workflows natively. We will add "
                  "a dedicated column if you name the report that needs one — question 2.",
    },
    "ANSI": {
        "title": "ANSI Z97.1",
        "code": "ANSI_IMPACT",
        "what": "The bag-drop safety-glazing test: pass or fail against a named class, "
                "with notes and photographs.",
        "derived": "**This is not the missile impact test.** It is a different procedure "
                   "with a different apparatus, and it is kept as its own requirement code "
                   "for exactly that reason — a reader who assumed \"impact\" meant one "
                   "thing would route it to the wrong test.\n\n"
                   "The proposal supplies the class — *Class A* — and nothing else. No rig "
                   "hardware is involved.",
        "cannot": "**ANSI Z97.1 is normally performed first on a specimen, and LabOS does "
                  "not enforce that.** The expectation is recorded; the other tests are not "
                  "blocked if it has not been done. A hard block would eventually stop "
                  "legitimate work and there is no override in this design — question 1.\n\n"
                  "As with Forced Entry, there is no dedicated `ANSI Result` column; the "
                  "verdict is in `Test Result`.",
    },
}

ORDER = ["STATIC", "CYCLIC", "IMPACT", "FORCED_ENTRY", "ANSI"]

# Spelled out, because "The 6 answers we need" reads like a form field and this
# document is asking a person for a decision.
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
         7: "seven", 8: "eight", 9: "nine"}

QUESTIONS = [
    ("ANSI ordering is recorded but not enforced",
     "ANSI Z97.1 is normally first on a specimen. LabOS records that and does not block the "
     "others. Is informational-only correct, or do you want it enforced — knowing a hard "
     "block has no override and will eventually stop legitimate work?"),
    ("Forced Entry and ANSI share one result column",
     "Both verdicts land in `Test Result`, with detail in `Notes` and the JSON, rather than "
     "in dedicated `Forced Entry Result` and `ANSI Result` columns. Is that enough, or does "
     "a named report need them separately?"),
    ("The target impact velocity is never shown to the operator",
     "It is read from the proposal and frozen onto the attempt, so it is in the record, but "
     "it is not on the screen at the rig. Does the operator need to see it?"),
    ("Impact location stays a LabOS-side observation",
     "LabOS records where each impact landed; Airtable has no field for it, because location "
     "is an observation and not a requirement — and an unwanted field in a shared base is "
     "much harder to remove than to add. Confirm that is right."),
    ("`Static / Type` is read and then changes nothing",
     "The proposal's static programme value — *Full* — is read and validated, but LabOS "
     "derives the same six-stage programme regardless. If a proposal ever specified a "
     "different programme, LabOS would run the full one without saying so. Is *Full* the "
     "only static programme in practice? If not, we should make LabOS refuse the others out "
     "loud rather than ignore them."),
    ("A five-impact test will appear as five records in the Airtable base",
     "This follows from one attempt per impact: attempts are what we publish, so five impacts are five rows, "
     "each with its own verdict and photographs, where today they are one row with a summary line. It changes "
     "what the Airtable team's views, groupings and automations see, and it needs one field added on their "
     "side — `Impact Number` — so a roll-up can count five impacts of one test rather than five tests. "
     "Confirm that is what you intend, because it is the half of the instruction that lands on somebody "
     "else's base.",
     "**Confirmed 2026-09-08.** So `Impact Number` is being added to the Airtable schema, and the Airtable "
     "team is told before the change goes to their production base."),
]


# --- composition -----------------------------------------------------------

def _dest(row):
    """How to name the outbound destination — the state decides, not the field.

    A row whose state is `UNMET-*` names an Airtable field that exists and that
    LabOS does **not** write. Printing the field name alone would tell the
    reader we send it, which is the opposite of what the page says two
    paragraphs later.
    """
    state, field = row["state"], (row["outbound_field"] or "").strip()
    if state.startswith("UNMET"):
        return "**not sent yet** — see below"
    if state.startswith("GAP"):
        return "**not built yet** — see below"
    if state == "OUT-OF-SCOPE":
        return "**out of scope for this release**"
    if field in ("", "-"):
        return "— *(stays in LabOS)*"
    if field == "JSON only":
        return "*in the JSON response*"
    return f"`{field}`"


def _source(row):
    """Where it lives locally, or an honest dash when nothing holds it."""
    store = (row["local_storage"] or "").strip()
    if store in ("", "-"):
        return "— *(nothing holds it)*"
    if store == "(not in Airtable)":
        return "— *(LabOS only)*"
    return f"`{store}`"


def _flag(row):
    """A short parenthetical when a row is not simply fine."""
    state = row["state"]
    if state.startswith("UNMET") or state.startswith("GAP") or state == "OUT-OF-SCOPE":
        return f" *({state.lower().replace('-', ' ')})*"
    return ""


def _pressures(pair, factors, directions):
    """A worked example row: the pair times each factor, in its direction."""
    inward, outward = pair
    return [round((inward if d == "inward" else outward) * f, 2)
            for f, d in zip(factors, directions)]


def build():
    static = _class_literals(APP / "domain" / "static_test_pressure_calculator.py")
    cyclic = _class_literals(APP / "domain" / "cyclic_test_pressure_calculator.py")
    reqs = _literals(APP / "airtable" / "requirements.py")
    imp = _literals(APP / "airtable" / "importer.py")
    hold = _static_hold_seconds()
    options = _test_type_options()
    rows, by_type = _rows(DOCS / "evidence" / "business-io-reconciliation-2026-09-08"
                          / "reconciliation.csv", "test_type")
    reg_rows, _ = _rows(REGISTER, "airtable_field")
    phase = {r["airtable_field"]: r["write_phase"] for r in reg_rows}

    s_factors = static["STATIC_PRESSURE_FACTOR"]
    s_dirs = ["inward" if i % 2 == 0 else "outward" for i in range(len(s_factors))]
    c_high, c_low = cyclic["HIGH_PRESSURE_FACTORS"], cyclic["LOW_PRESSURE_FACTORS"]
    c_counts = cyclic["CYCLE_COUNT"]
    c_dirs = ["inward" if i < len(c_high) // 2 else "outward" for i in range(len(c_high))]
    kinds, units = reqs["KIND_BY_CODE"], reqs["UNITS_BY_KIND"]
    executable = sorted(reqs["EXECUTABLE_CODES"])
    local_type = imp["LOCAL_TYPE_BY_CODE"]

    L = []
    w = L.append

    w("# LabOS — what each of the five tests requires, and what it produces")
    w("")
    w("**For approval by the project owner. Draft, not sent.** Every number and field name in this")
    w("document is generated from the LabOS source — the stage factors are read out of the calculators")
    w("that multiply them, the routing out of the importer that routes on it. It cannot describe a field")
    w("we do not have.")
    w("")
    w("**Why you are being asked now.** The three new tests are built and the UI developer's next piece")
    w("of work is the screens for them. This is the last point at which a correction is a database change")
    w("rather than a database change plus a UI rewrite plus a migration against live rows. The Airtable")
    w("change document is written, verified against both live bases, and **held until you have approved")
    w("this**.")
    w("")
    w("---")
    w("")

    # -- page 1: the summary ------------------------------------------------
    w("## What you are approving")
    w("")
    w(f"The five test types LabOS runs — the `Test Type` option set, verbatim: "
      + " · ".join(f"**{o}**" for o in options) + ".")
    w("")
    w("| Test | What the proposal must supply | What LabOS produces |")
    w("|---|---|---|")
    summary = {
        "STATIC": (f"one inward/outward design-pressure pair (PSF)",
                   f"{len(s_factors)} stages, {hold} s hold each, deflection readings per gauge"),
        "CYCLIC": ("the same pair — nothing further",
                   f"{len(c_high)} stages, {sum(c_counts):,} cycles in total"),
        "IMPACT": ("how many impacts — the classification is chosen in LabOS",
                   "one attempt per impact — each with its own pass/fail, photographs and verdict"),
        "FORCED_ENTRY": ("the grade to judge against — no numbers",
                         "one pass/fail verdict per attempt, with notes and photographs"),
        "ANSI": ("the class to judge against — no numbers",
                 "one pass/fail verdict per attempt, with notes and photographs"),
    }
    for key in ORDER:
        needs, makes = summary[key]
        w(f"| **{PROSE[key]['title']}** | {needs} | {makes} |")
    w("")
    w("**What you are approving:** that this is the right set of information to require, to record and to")
    w("send back — per test. Not the screens, not the schedule, and not the Airtable team's own fields.")
    w("")
    w("**What you are not approving:** anything that changes a production rig. None of the three new")
    w("tests touch rig hardware, there is no firmware change in this work, and nothing is deployed.")
    w("")
    open_qs = len([q for q in QUESTIONS if len(q) < 3 or not q[2]])
    w(f"**{WORDS[len(QUESTIONS)].capitalize()} answers are needed rather than a general yes** — "
      f"**{WORDS[open_qs]} still open** — listed after the five")
    w("pages, and each is a place where we made a call you may not want.")
    w("")
    w("Approved by: ______________________________   Date: ______________")
    w("")
    w("---")
    w("")

    # -- the pre-fill page: the product owner's own principle ---------------
    w("## Nothing the proposal already says is retyped")
    w("")
    w("> *\"The operator should not have to manually recreate information that already exists in HubSpot or")
    w("> Airtable.\"*")
    w("")
    w("This is the principle these pages are built on, so it is worth stating what it amounts to in fields")
    w("rather than in intent. **Every row marked *from the proposal* on the five pages that follow is a field")
    w("the operator never types.**")
    w("")
    prefill = [r for r in rows if r["direction"] == "IN"]
    per = {}
    for r in prefill:
        per.setdefault(r["test_type"], []).append(r)
    live = [r for r in prefill if r["state"].startswith("OK")]
    w(f"**{len(live)} fields are read from Airtable and pre-filled today**, out of {len(prefill)} the")
    w("integration reads in total — and that total is the whole of it. The read boundary is a list in the")
    w("code, not a convention: a field not on the list is not copied, which is also how the commercial")
    w("fields stay out.")
    w("")
    w("| Where it applies | Pre-filled from Airtable |")
    w("|---|---|")
    labels = {
        "ALL": "Every test — the job, specimen, protocol and requirement identity",
        "GAUGE_COUNT": "How many deflection gauges *(a parameter, not a test)*",
        "WATER": "Water infiltration *(deferred — see the appendix)*",
    }
    for key in ["ALL"] + ORDER + ["GAUGE_COUNT", "WATER"]:
        if key not in per:
            continue
        label = labels.get(key) or PROSE[key]["title"]
        seen, cells = set(), []
        for r in per[key]:
            if r["airtable_field"] in seen:
                continue
            seen.add(r["airtable_field"])
            cells.append(f"`{r['airtable_field']}`{_flag(r)}")
        w(f"| {label} | {' · '.join(cells)} |")
    w("")
    if len(live) != len(prefill):
        w("The three carrying a note are read but not yet fully acted on: the target impact velocity does not")
        w("reach the operator (question 3), the gauge count is not reconciled against what the rig takes at")
        w("start, and water infiltration is out of scope for this release. **None of them is a field anybody")
        w("retypes** — they are read; what is incomplete is what we do with them afterwards.")
        w("")
    w("The design-pressure pair is the one worth pointing at twice: **two numbers pre-filled become fourteen")
    w("test stages**, none of which anybody types or checks.")
    w("")
    w("**What the operator does still enter is not information that already exists** — it is what the test")
    w("produced: the outcomes, the measurements, the notes, the photographs, the verdict, and which rig ran")
    w("it. None of that is in Airtable to be recreated.")
    w("")
    w("**On HubSpot specifically.** LabOS does not read HubSpot and does not need to. Contract §1 sets the")
    w("boundary — *HubSpot supplies approved commercial scope; Airtable owns the assigned")
    w("project/specimen/protocol hierarchy* — and HubSpot's own identity already arrives in Airtable on")
    w("`IFET Projects` as `Hubspot Deal ID` and `Hubspot Deal Stage`. So anything from HubSpot that an")
    w("operator would otherwise retype reaches us through Airtable, and what does not reach us is commercial")
    w("data a rig has no use for. **If you expect LabOS to read HubSpot directly, that is a new scope item")
    w("and not a gap in this document** — say so and it gets planned rather than assumed.")
    w("")
    w("---")
    w("")

    # -- pages 2-6: one per test -------------------------------------------
    for key in ORDER:
        p = PROSE[key]
        rows = by_type.get(key, [])
        code = p["code"]
        first_code = code.split(" · ")[0]
        w(f"## {p['title']}")
        w("")
        w(f"*Requirement code `{code}` · read as "
          f"{kinds[first_code]}"
          + (f", unit {' or '.join(sorted(u for u in units[kinds[first_code]] if u))}"
             if any(units[kinds[first_code]]) else ", no unit")
          + f" · becomes `{local_type.get(first_code, '—')}` work in LabOS*")
        w("")
        w(p["what"])
        w("")

        w("### What the proposal must supply")
        w("")
        ins = [r for r in rows if r["direction"] == "IN"]
        if ins:
            w("| Requirement | Airtable field | Where it lands in LabOS |")
            w("|---|---|---|")
            for r in ins:
                w(f"| {r['requirement']}{_flag(r)} | `{r['airtable_field']}` "
                  f"| {_source(r)} |")
        else:
            w("*Nothing beyond the design-pressure pair named on the Static Load page — this test is")
            w("generated from it.*")
        w("")
        w("A requirement LabOS cannot read unambiguously is **refused and reported to the operator with")
        w("the reason**, never guessed at. A blank is never read as zero, and a unit that disagrees with")
        w("the requirement is treated as a different test rather than a typo.")
        w("")

        w("### What LabOS works out for itself")
        w("")
        w(p["derived"])
        w("")
        if key == "STATIC":
            w(f"The six stages, as factors of the pair — and worked through for a 60 / 45 PSF pair:")
            w("")
            w("| Stage | Direction | Factor | 60 / 45 gives | Hold |")
            w("|---|---|---|---|---|")
            example = _pressures((60, 45), s_factors, s_dirs)
            for i, (f, d, v) in enumerate(zip(s_factors, s_dirs, example), 1):
                w(f"| {i} | {d} | ×{f} | {v} PSF | {hold} s |")
            w("")
        if key == "CYCLIC":
            w("The eight stages — and worked through for the same 60 / 45 PSF pair:")
            w("")
            w("| Stage | Direction | High | Low | Cycles | 60 / 45 gives (high) |")
            w("|---|---|---|---|---|---|")
            high_ex = _pressures((60, 45), c_high, c_dirs)
            for i, (h, lo, c, d, v) in enumerate(
                    zip(c_high, c_low, c_counts, c_dirs, high_ex), 1):
                w(f"| {i} | {d} | ×{h} | ×{lo} | {c:,} | {v} PSF |")
            w("")
            w(f"**{sum(c_counts):,} cycles in total.**")
            w("")

        locals_ = [r for r in rows if r["direction"] == "LOCAL"]
        outs = [r for r in rows if r["direction"] == "OUT"]
        if locals_ or outs:
            w("### What is recorded, and what reaches Airtable")
            w("")
            w("| What | Kept in LabOS as | Sent to Airtable as | When |")
            w("|---|---|---|---|")
            for r in locals_ + outs:
                when = phase.get(r["airtable_field"], r["write_phase"]) or "—"
                if r["state"].startswith(("UNMET", "GAP")):
                    when = "—"
                w(f"| {r['requirement']} | {_source(r)} | {_dest(r)} | {when} |")
            w("")
            if outs and all(r["state"].startswith(("UNMET", "GAP")) for r in outs):
                w("**Everything specific to this test is currently withheld** — the reasons are below. What")
                w("does reach Airtable for it is on the last page: the attempt, its verdict, who ran it, the")
                w("photographs and the full JSON. The test is published; its measurements are not.")
            else:
                w("This is what is specific to this test. Everything sent on *every* attempt — the verdict,")
                w("the times, the operator, the photographs, the full JSON — is on the last page.")
            w("")

        w("### What we cannot do yet, and why")
        w("")
        w(p["cannot"])
        w("")
        w("---")
        w("")

    # -- the same-for-every-test page ---------------------------------------
    all_rows = by_type.get("ALL", [])
    w("## The same for every test")
    w("")
    w("None of this is per-test, and none of it is anything you have to supply — it is what LabOS")
    w("records around every attempt so that a result can be traced back to the requirement that asked")
    w("for it, and to the person who ran it.")
    w("")
    w("### What identifies the work")
    w("")
    w("| What | Read from Airtable as |")
    w("|---|---|")
    for r in [r for r in all_rows if r["direction"] == "IN"]:
        w(f"| {r['requirement']} | `{r['airtable_field']}` |")
    w("")
    w("**Routing is by record id, never by name.** A renamed job or section still points at the same")
    w("work, and a job number typed twice cannot silently re-point one job's results at another.")
    w("")
    w("### What LabOS sends back on every attempt")
    w("")
    w("| What | Sent as | When |")
    w("|---|---|---|")
    for r in [r for r in all_rows if r["direction"] == "OUT"]:
        when = phase.get(r["airtable_field"], r["write_phase"]) or "—"
        if r["state"].startswith(("UNMET", "GAP")):
            when = "—"
        w(f"| {r['requirement']} | {_dest(r)} | {when} |")
    w("")
    w("*When* is the moment the value is written: **create** when the attempt starts, **terminal** when")
    w("it finishes or is aborted, **verdict** when a reviewer first judges it, **attachment** when a")
    w("photograph settles. A value is never sent blank — an absent value is left out of the write")
    w("entirely, so an empty cell in Airtable never has to be read as a decision.")
    w("")
    w("**Two rows say \"not built yet\".** Corrections — superseding an attempt rather than editing it —")
    w("are designed and specified but have no route yet: the columns exist in Airtable and LabOS has the")
    w("fields, and what is missing is the operator path that creates a correction. Until then a")
    w("correction cannot be recorded as one.")
    w("")
    w("---")
    w("")

    # -- page 7: the questions and the appendix ----------------------------
    w(f"## The {WORDS[len(QUESTIONS)]} answers we need")
    w("")
    for i, question in enumerate(QUESTIONS, 1):
        head, body, answer = (*question, None)[:3]
        w(f"**{i}. {head}.** {body}")
        w("")
        if answer:
            w(f"> {answer}")
            w("")
    w("---")
    w("")
    w("## Appendix — five tests, nine requirement codes")
    w("")
    w("The proposals carry nine requirement codes and LabOS runs five tests. The difference is not an")
    w("omission:")
    w("")
    w("| Code | Kind | What LabOS does with it |")
    w("|---|---|---|")
    notes = {
        "GAUGE_COUNT": "**A parameter, not a test.** How many deflection gauges the setup uses. "
                       "Stored on the project; produces no test of its own",
        "STATIC_PROGRAMME": "**A parameter, not a test.** Which static programme the proposal "
                            "names, e.g. *Full*. Read and validated, and currently changes "
                            "nothing — see question 5",
        "WATER_PRESSURE": "**Deferred.** Water infiltration is out of scope for this release. "
                          "Known to LabOS so a section carrying it is reported as "
                          "non-executable, rather than hitting the unknown-code refusal",
    }
    for code in sorted(kinds):
        if code in executable:
            what = f"Runs as **{PROSE[[k for k in ORDER if PROSE[k]['code'].startswith(code) or code in PROSE[k]['code'].split(' · ')][0]]['title']}**"
        else:
            what = notes[code]
        w(f"| `{code}` | {kinds[code]} | {what} |")
    w("")
    w("**Routing is by code, never by section name.** Renaming a section in Airtable can therefore never")
    w("silently change which procedure gets run, and a code LabOS does not know stays visible but cannot")
    w("start a test.")
    w("")
    w("---")
    w("")
    w("*Generated from the LabOS source by `app/airtable/test_requirements_doc.py`. Regenerate rather")
    w("than edit: `check_register.py` fails when this document and the code have parted company.*")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the written document is not current")
    args = ap.parse_args(argv)
    fresh = build()
    if args.check:
        if not OUT.exists():
            print(f"MISSING  {OUT}")
            return 1
        if OUT.read_text() != fresh:
            print(f"STALE    {OUT}\n         regenerate: python3 {HERE.name}")
            return 1
        print(f"current  {OUT.name}")
        return 0
    OUT.write_text(fresh)
    print(f"wrote {OUT} ({len(fresh.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
