"""Write contract v0.3 §4, as data.

The prose contract lives in `ifet-firmware/docs/labos-airtable-write-contract-v0.3.md`.
This module is the machine-checkable half: the probe diffs the live Airtable
schema against it, and the payload builder validates against it. Keeping one
copy in code and one in prose is a drift risk, so the rule is the same as for
the docs — **the prose is authoritative; this file is a view of it.** If they
disagree, the prose wins and this file is a bug.

`v2` records what the Airtable team's API Integration Guide v2 (2026-08-17)
actually publishes, so the probe can tell three different situations apart:
a field we never asked for, a field we asked for and did not get, and a field
we were promised that has not been created.
"""

CONTRACT_VERSION = "0.3"

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
                 "option_wire", "note")

    def __init__(self, labos_name, req, v2, kind, wire_name=None, options=None,
                 option_wire=None, note=""):
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
    Field("Corrects Attempt ID", CONDITIONAL, ABSENT, "text",
          note="§10.14 BLOCKING — automations must branch on this"),
    Field("Correction Reason", CONDITIONAL, ABSENT, "long text", note="§10.14 BLOCKING"),

    # 4.2 wall snapshot — absent from v2 in full
    Field("Airtable Wall ID", CONDITIONAL, ABSENT, "text", note="§10.15"),
    Field("Wall Name", CONDITIONAL, ABSENT, "text", note="§10.15"),
    Field("Airtable Wall Position ID", CONDITIONAL, ABSENT, "text", note="§10.15"),
    Field("Wall Position Name", CONDITIONAL, ABSENT, "text", note="§10.15"),
    Field("Wall Reservation Start Date", CONDITIONAL, ABSENT, "date", note="§10.15"),
    Field("Wall Reservation End Date", CONDITIONAL, ABSENT, "date", note="§10.15"),

    # 4.3 descriptors
    Field("Test Name", REQUIRED, ABSENT, "text", note="§10.15 — asked for as first-class"),
    Field("Test Type", REQUIRED, PRESENT, "single select",
          options=["Static Load", "Cycles", "Impact", "Forced Entry", "ANSI Z97.1"],
          note="§10.17 — the live base offers ONLY 'Static Load'; the other four "
               "have no option to land in and will 422. BLOCKING."),
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
          note="§10.15 — asked for as first-class"),

    # 4.4 measurements
    Field("Measured Value", CONDITIONAL, PRESENT, "number"),
    Field("Unit", CONDITIONAL, PRESENT, "single select",
          options=["PSF", "PSI", "in", "mm", "lbf", "N", "cycles", "s"]),
    Field("Max Pressure Achieved", CONDITIONAL, PRESENT, "number", note="granted in v2"),
    Field("Deflection Value", CONDITIONAL, PRESENT, "number"),
    Field("Deflection Unit", CONDITIONAL, PRESENT, "single select",
          options=["in", "mm"], note="granted in v2"),
    Field("Required Value", CONDITIONAL, ABSENT, "number", note="§10.15 — carry in JSON"),
    Field("Required Unit", CONDITIONAL, ABSENT, "single select", note="§10.15 — carry in JSON"),
    Field("Cycles Required", CONDITIONAL, ABSENT, "number", note="§10.15 — carry in JSON"),
    Field("Cycles Completed", CONDITIONAL, ABSENT, "number", note="§10.15 — carry in JSON"),
    Field("Impact Result", CONDITIONAL, PRESENT, "text/single select",
          note="§10.5 — option set unknown until probed"),
    Field("Result Detail (JSON)", CONDITIONAL, RENAMED, "long text",
          wire_name="Complete LabOS JSON Response",
          note="granted in v2 — the extensibility valve, §6"),

    # 4.5 timing, people, disposition
    Field("Testing Start Date", REQUIRED, ABSENT, "datetime",
          note="§10.13 BLOCKING — collapsed into their 'Test Date'"),
    Field("Testing End Date", CONDITIONAL, ABSENT, "datetime",
          note="§10.13 BLOCKING — collapsed into their 'Test Date'"),
    Field("Test Date", CONDITIONAL, PRESENT, "date/datetime",
          note="v2's replacement for the two above; LabOS writes the START instant"),
    Field("Operator Name", REQUIRED, PRESENT, "text"),
    Field("Retest Required", REQUIRED, PRESENT, "checkbox",
          note="explicit true/false; omission must not read as false"),
    Field("Testing Continued", CONDITIONAL, PRESENT, "single select",
          options=["Continued", "Stopped"]),
    Field("Notes", OPTIONAL, PRESENT, "long text"),

    # 4.6 artifacts & metadata
    Field("Photos", OPTIONAL, PRESENT, "url/long text",
          note="§10.1 — must NOT be an Attachment field"),
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
BLOCKING_ABSENT = ("Corrects Attempt ID", "Correction Reason")
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
READ_SIDE_EXPECTED = (
    "Design Pressure Inward (PSF)",
    "Design Pressure Outward (PSF)",
    "Hold Time (s)",
    "Cycles Required",
    "Deflection Points",
    "Loading Sequence (JSON)",
    "Required Testing Parameters (JSON)",
)


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
REQUIRED_BY_TEST_TYPE = {
    STATIC_LOAD: ("Measured Value", "Unit", "Max Pressure Achieved",
                  "Deflection Value", "Deflection Unit", "Test Result",
                  "Result Detail (JSON)", "Required Value", "Required Unit"),
    CYCLES: ("Measured Value", "Unit", "Max Pressure Achieved",
             "Deflection Value", "Deflection Unit", "Cycles Required",
             "Cycles Completed", "Test Result", "Result Detail (JSON)",
             "Required Value", "Required Unit"),
    IMPACT: ("Impact Result", "Test Result", "Result Detail (JSON)", "Photos"),
    FORCED_ENTRY: ("Test Result", "Result Detail (JSON)", "Photos"),
    ANSI_Z97: ("Test Result", "Result Detail (JSON)", "Photos"),
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
# `Testing End Date` is required here even though guide v2 deleted the column
# (§10.13): the contract requires the value, and the builder routes it into
# `Complete LabOS JSON Response` rather than dropping it. Same for `Test Name`
# above. Requirements are about the DATA, not about which column happens to
# exist this week — otherwise an Airtable schema decision quietly reduces what
# a completed attempt has to prove.
TERMINAL_REQUIRED = ("Operator Name", "Retest Required", "Testing Continued",
                     "Testing End Date")
