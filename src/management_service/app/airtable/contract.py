"""Write contract v0.4 §6, as data.

The prose contract lives in `ifet-firmware/docs/labos-airtable/contract/write-contract-v0.4.md`.
This module is the machine-checkable half: the probe diffs the live Airtable
schema against it, and the payload builder validates against it. Keeping one
copy in code and one in prose is a drift risk, so the rule is the same as for
the docs — **the prose is authoritative; this file is a view of it.** If they
disagree, the prose wins and this file is a bug.

`v2` originally recorded what the Airtable team's API Integration Guide v2
(2026-08-17) published. It now records **whether the field exists in the base**,
which is the only question the probe needs answered, and it is set from the live
schema rather than from a document: `PRESENT` means the 2026-09-06 baseline has
it, `ABSENT` means it does not and deliberately will not.

**Presence is per base.** The 14 fields applied on 2026-09-06 exist in Testing
`app4oXS3Kd5IKWgJ7` and not yet in production `app0OCunbmuXl7Hc9` — production
gets them when the Airtable team applies the change document. So `EXPECTED_LIVE`
describes the Testing base; against production, the 14 read as not-yet-applied
rather than as a contract violation.

**Decision A9 narrowed the read side to identity.** LabOS reads no Airtable
requirement values at all, so `READ_SIDE_EXPECTED` below is the whole inbound
surface, and fields such as `Required Value Inward` exist in the base while LabOS
never touches them.
"""

CONTRACT_VERSION = "0.4"

# --- v2 status -------------------------------------------------------------
PRESENT = "present"      # in their v2 raw table under this exact name
RENAMED = "renamed"      # in their v2 raw table under a different name
ABSENT = "absent"        # not in their v2 raw table — requested

# --- requirement -----------------------------------------------------------
REQUIRED = "R"
CONDITIONAL = "C"
OPTIONAL = "O"


class Field:
    __slots__ = ("labos_name", "wire_name", "req", "v2", "kind", "options",
                 "option_wire", "note", "omitted")

    def __init__(self, labos_name, req, v2, kind, wire_name=None, options=None,
                 option_wire=None, note="", omitted=False):
        self.labos_name = labos_name
        # What actually goes on the wire. Where the Airtable team chose a
        # different name, theirs wins — the LabOS name is internal vocabulary.
        self.wire_name = wire_name or labos_name
        self.req = req
        self.v2 = v2
        self.kind = kind
        self.options = tuple(options or ())
        # Same principle one level down: where their single-select spells an
        # option differently, theirs wins on the wire and LabOS keeps its own
        # word internally. Confirmed against the live base by the schema probe
        # on 2026-08-23 — these are transcribed from the base, not proposed.
        self.option_wire = dict(option_wire or {})
        self.note = note
        # A9/A2/A3: the column exists and LabOS must never write it. Distinct
        # from ABSENT, which is about the base not having the field at all.
        # An omitted field is a decision; an absent one is a fact.
        self.omitted = omitted

    def wire_option(self, value):
        """Translate a LabOS option to the spelling the base actually holds."""
        return self.option_wire.get(value, value)

    @property
    def expected_live(self):
        """Should this field exist in the base right now?"""
        return self.v2 in (PRESENT, RENAMED)

    def __repr__(self):
        return f"Field({self.wire_name!r}, {self.req}, {self.v2})"


# --------------------------------------------------------------- the envelope
# Order follows contract §4.1 → §4.6.

FIELDS = [
    # 4.1 identity & linkage
    Field("Airtable Project ID", REQUIRED, PRESENT, "text", note="§10.2 text vs link-to-record"),
    Field("Airtable Mock-Up ID", REQUIRED, RENAMED, "text", wire_name="Airtable Mockup ID",
          note="their spelling drops the hyphen"),
    Field("Airtable Protocol ID", REQUIRED, PRESENT, "text"),
    Field("Airtable Section ID", REQUIRED, PRESENT, "text"),
    Field("LabOS Test ID", REQUIRED, PRESENT, "text"),
    Field("LabOS Attempt ID", REQUIRED, PRESENT, "text",
          note="MERGE KEY — must be plain text, not a computed field"),
    Field("Attempt Number", REQUIRED, PRESENT, "number"),
    Field("Schema Version", REQUIRED, PRESENT, "text", note="granted in v2"),
    Field("Corrects Attempt ID", CONDITIONAL, PRESENT, "text",
          note="APPLIED 2026-09-06 — no longer blocking. Automations must branch on this"),
    Field("Correction Reason", CONDITIONAL, PRESENT, "long text",
          note="the Airtable team added this themselves; found by the 2026-09-05 baseline diff"),

    # 4.2 wall snapshot — RETIRED. The six wall fields were never created and are
    # not wanted: scheduling and wall assignment are Airtable's operational
    # concern and never cross the boundary (contract §1). `Walls & Positions` and
    # `Wall Scheduling/Reservation` are two of the three tables LabOS ignores
    # entirely.

    # 4.3 descriptors
    Field("Test Name", REQUIRED, ABSENT, "text",
          note="JSON-only by §6, by decision rather than by omission — it is a "
               "display string, and a column would invite filtering on a name "
               "instead of on Requirement Code"),
    Field("Test Type", REQUIRED, PRESENT, "single select",
          options=["Static Load", "Cycles", "Impact", "Forced Entry", "ANSI Z97.1"],
          note="§10.17 CLOSED — the 2026-09-05 baseline found all five options "
               "live in BOTH bases. They shipped it without telling us, which is "
               "why the baseline is diffed rather than trusted."),
    Field("Test Status", REQUIRED, PRESENT, "single select",
          options=["In Progress", "Completed", "Aborted"],
          option_wire={"Aborted": "Abborted"},
          note="§10.18 — 'Abborted' is misspelt in their base. LabOS sends their "
               "spelling verbatim, because a single-select will not accept ours."),
    Field("Test Result", CONDITIONAL, PRESENT, "single select",
          options=["Pass", "Fail", "Inconclusive"],
          option_wire={"Pass": "Passed", "Fail": "Failed"},
          note="§10.16 — CLOSED by the probe: the base holds Passed/Failed, so "
               "their v2 example was right and this contract was wrong."),
    Field("Abort Reason", CONDITIONAL, ABSENT, "single select",
          options=["Specimen Failure", "Equipment Fault", "Operator Stop",
                   "Power/Comms Loss", "Other"],
          note="JSON-only by §6, by decision. A single-select here would need "
               "their agreement on the option set for no operational gain"),

    # 4.4 measurements
    Field("Measured Value", CONDITIONAL, PRESENT, "number"),
    Field("Unit", CONDITIONAL, PRESENT, "text",
          options=["PSF", "PSI", "in", "mm", "lbf", "N", "cycles", "s"],
          note="the base holds this as singleLineText, not a select (verified "
               "2026-09-06). The option list is OUR vocabulary and is enforced "
               "by us before sending - Airtable will accept anything here, which "
               "is exactly why we validate rather than relying on the field type"),
    Field("Max Pressure Achieved", CONDITIONAL, PRESENT, "number", omitted=True,
          note="A2 — OMITTED. The rig sends deflections[] and nothing else, so "
               "there is no achieved-pressure source at all. The setpoint is a "
               "target, and a target is never an achieved value. Milestone M7"),
    Field("Deflection Value", CONDITIONAL, PRESENT, "number", omitted=True,
          note="A3 — OMITTED. Raw IO-Link counts mislabelled as inches; "
               "uncalibrated evidence stays local. Milestone M6"),
    Field("Deflection Unit", CONDITIONAL, PRESENT, "text",
          options=["in", "mm"], omitted=True,
          note="A3 — OMITTED with the value it would qualify. singleLineText in "
               "the base, not a select (verified 2026-09-06)"),
    # NOT omitted, and the distinction matters. A9 stops LabOS *reading*
    # requirement values from Airtable; it does not stop LabOS publishing the
    # ones its own operator entered. These are LabOS's numbers, which is exactly
    # why they can be trusted - unlike the Airtable values, they did not come
    # through the extractor. Carried in the JSON valve; no column exists.
    Field("Required Value", CONDITIONAL, ABSENT, "number",
          note="§10.15 — carry in JSON. The operator-entered requirement the "
               "attempt actually ran against, so management can see what was "
               "targeted. Not an echo of an Airtable value"),
    Field("Required Unit", CONDITIONAL, ABSENT, "single select",
          note="§10.15 — carry in JSON, with the value it qualifies"),
    Field("Cycles Required", CONDITIONAL, ABSENT, "number", note="§10.15 — carry in JSON"),
    Field("Cycles Completed", CONDITIONAL, ABSENT, "number", note="§10.15 — carry in JSON"),
    Field("Impact Result", CONDITIONAL, PRESENT, "text/single select",
          note="§10.5 — option set unknown until probed"),
    Field("Result Detail (JSON)", CONDITIONAL, RENAMED, "long text",
          wire_name="Complete LabOS JSON Response",
          note="granted in v2 — the extensibility valve, §6"),

    # 4.5 timing, people, disposition
    Field("Testing Start Date", REQUIRED, PRESENT, "datetime",
          note="APPLIED 2026-09-06 — no longer collapsed into Test Date"),
    Field("Testing End Date", CONDITIONAL, PRESENT, "datetime",
          note="APPLIED 2026-09-06"),
    Field("Test Date", CONDITIONAL, PRESENT, "datetime",
          note="§10.20 CLOSED — now a dateTime in both bases. **Means COMPLETION** "
               "under v0.4, not the start instant as v0.3 had it. Absent at create, "
               "equal to completion at terminal"),
    Field("Operator Name", REQUIRED, PRESENT, "text"),
    Field("Retest Required", REQUIRED, PRESENT, "checkbox",
          note="explicit true/false; omission must not read as false"),
    Field("Testing Continued", CONDITIONAL, PRESENT, "text",
          options=["Continued", "Stopped"],
          note="singleLineText in the base, not a select (verified 2026-09-06)"),
    Field("Notes", OPTIONAL, PRESENT, "long text"),
    Field("LabOS Verdict By", CONDITIONAL, PRESENT, "text",
          note="APPLIED 2026-09-06 — the named reviewer. Stored separately from "
               "the operator even when they are the same person: §4 needs to know "
               "who signed off, not just who ran it"),
    Field("LabOS Verdict At", CONDITIONAL, PRESENT, "datetime",
          note="APPLIED 2026-09-06 — when the first review happened, which is not "
               "when the test finished"),

    # 4.6 artifacts & metadata
    Field("Photos", OPTIONAL, PRESENT, "url/long text",
          note="§10.1 — the LEGACY url field. Must NOT be an Attachment field; "
               "originals stay in LabOS"),
    Field("LabOS Photos", OPTIONAL, PRESENT, "attachment", omitted=False,
          note="APPLIED 2026-09-06 — the preview channel, and deliberately an "
               "attachment where `Photos` deliberately is not. Previews go to "
               "Airtable; originals never leave LabOS (§6)"),
    Field("Excel File Link", OPTIONAL, PRESENT, "url"),
    Field("Report Link", OPTIONAL, RENAMED, "url", wire_name="LabOS Report Link"),
    Field("LabOS Created At", REQUIRED, PRESENT, "datetime"),
    Field("LabOS Updated At", REQUIRED, PRESENT, "datetime",
          note="doubles as the arrival/staleness signal, §8"),
    Field("Test Rig", OPTIONAL, ABSENT, "single select",
          options=["System 1", "System 2"], note="§10.15 — carry in JSON"),
    Field("LabOS Version", OPTIONAL, ABSENT, "text", note="§10.15 — carry in JSON"),
    Field("Result Rationale", OPTIONAL, ABSENT, "long text", note="§10.15 — carry in JSON"),
]

BY_WIRE_NAME = {f.wire_name: f for f in FIELDS}
BY_LABOS_NAME = {f.labos_name: f for f in FIELDS}

# Fields the base should already contain, per v2.
EXPECTED_LIVE = tuple(f.wire_name for f in FIELDS if f.expected_live)

# Fields we have asked for and not yet received, split by whether the JSON
# valve can carry them. Only the blocking two genuinely cannot.
# Both former blockers were applied on 2026-09-06, so this is now empty. Kept as
# a named constant because the probe and the tests assert against it: an empty
# tuple is the statement "nothing we need is missing from the base", which is a
# different claim from the constant having been deleted.
BLOCKING_ABSENT = ()
REQUESTED_ABSENT = tuple(
    f.wire_name for f in FIELDS
    if f.v2 == ABSENT and f.wire_name not in BLOCKING_ABSENT
)

# Airtable field types that would violate the contract if we saw them.
FORBIDDEN_TYPES = {
    "Photos": ("multipleAttachments",),          # §7 — links, not attachments
    "LabOS Attempt ID": ("formula", "rollup", "lookup", "autoNumber"),  # §2 merge key
}

# Contract §9.1 — what a machine-readable Protocol Section looks like. The probe
# reports which of these exist; it cannot judge whether free text is "really"
# structured, which is exactly why §10.3 needs a human answer.
# A9 replaced this wholesale. It used to describe the machine-readable Protocol
# Section LabOS hoped for - design pressures, hold time, loading sequence - all
# of which LabOS would have executed from. It reads none of that now: the
# operator sets a test up in LabOS as they always have.
#
# What remains is identity, plus the one field that says which of the five tests
# a section is. Record IDs are read from every table and are not listed here
# because they are not fields.
# What makes a section MACHINE-READABLE. `Section Name` is deliberately not here:
# LabOS reads it, but it is display only and never routes work, so counting it
# would let a prose-only section pass as structured - which is exactly the §10.3
# failure this check exists to catch.
READ_SIDE_EXPECTED = (
    "Requirement Code",      # which of the five tests this section is
    "Applicability",         # Required / Not Required / Unconfirmed
)

# Read for identity and display, per table. The join is `IFET job number` for
# people and the rec... record ID for machines - the number is hand-typed, so it
# must never be the only key.
READ_SIDE_BY_TABLE = {
    "IFET Projects": ("IFET job number", "Project name"),
    "Mock-Ups/Specimens": ("Project Name", "Mock-up/specimen name"),
    "Tests Protocols": ("Mock-Up", "Protocol Name"),
    "Protocol Sections": ("Test Protocol", "Section Name") + READ_SIDE_EXPECTED,
}


# ------------------------------------------------- §5 blank / null / sentinel
# Airtable rejects "" on number, date and single-select fields, so "send an
# empty string" is not implementable as a general rule. LabOS omits the key
# instead. `null` stays reserved for "explicitly clear a cell", which LabOS
# never does — records are immutable once terminal.
#
# 0 and False are DATA, not blanks. A 0 PSF reading is a measurement.
SENTINEL_VALUES = ("", "N/A", "n/a", "NA", "Not Available", "-", "--", "none", "None")

# ------------------------------------------------- §5.1 required by test type
STATIC_LOAD = "Static Load"
CYCLES = "Cycles"
IMPACT = "Impact"
FORCED_ENTRY = "Forced Entry"
ANSI_Z97 = "ANSI Z97.1"

TEST_TYPES = (STATIC_LOAD, CYCLES, IMPACT, FORCED_ENTRY, ANSI_Z97)

IN_PROGRESS = "In Progress"
COMPLETED = "Completed"
ABORTED = "Aborted"
TERMINAL_STATUSES = (COMPLETED, ABORTED)

# Required when `Test Status` is Completed. Keyed by LabOS field name; the
# builder resolves wire names itself. Mirrors contract §5.1 exactly — a field
# that is absent from the base is still required, it just travels inside
# `Complete LabOS JSON Response` instead of as a column (§10.15).
# Every field named here must be present and non-omitted on a completed write.
# A2, A3 and A9 removed five entries that v0.3 required, and the removals are the
# interesting part:
#
#   Max Pressure Achieved  - no source exists (A2). Requiring it would have made
#   Deflection Value/Unit  - uncalibrated, quarantined (A3)   every completed
#   Required Value/Unit    - not read from Airtable (A9)      Static Load
#                                                             unsendable.
#
# So a completed Static Load or Cycles row carries identity, type, status, dates,
# operator, Test Result, notes and the JSON - and **no number describing what
# physically happened**. That is the decided initial behaviour, it is the first
# thing the Airtable team will ask about, and M6/M7 are what change it.
REQUIRED_BY_TEST_TYPE = {
    STATIC_LOAD: ("Test Result", "Result Detail (JSON)"),
    CYCLES: ("Cycles Completed", "Test Result", "Result Detail (JSON)"),
    IMPACT: ("Impact Result", "Test Result", "Result Detail (JSON)", "LabOS Photos"),
    FORCED_ENTRY: ("Test Result", "Result Detail (JSON)", "LabOS Photos"),
    ANSI_Z97: ("Test Result", "Result Detail (JSON)", "LabOS Photos"),
}

# Always required, whatever the test type or status (§4.1).
ALWAYS_REQUIRED = (
    "Airtable Project ID", "Airtable Mock-Up ID", "Airtable Protocol ID",
    "Airtable Section ID", "LabOS Test ID", "LabOS Attempt ID",
    "Attempt Number", "Schema Version", "Test Type", "Test Status",
    "Test Name", "Testing Start Date", "LabOS Created At", "LabOS Updated At",
)

# Additionally required on a terminal write (§4.5).
#
# `Testing End Date` now has a real column (applied 2026-09-06), but the
# principle that put it here stands and is worth keeping: requirements are about
# the DATA, not about which column happens to exist this week. `Test Name` and
# `Abort Reason` are still required data carried inside
# `Complete LabOS JSON Response`, because otherwise an Airtable schema decision
# would quietly reduce what a completed attempt has to prove.
TERMINAL_REQUIRED = ("Operator Name", "Retest Required", "Testing Continued",
                     "Testing End Date")
