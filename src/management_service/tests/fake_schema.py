"""A synthetic Airtable base schema, shaped like the live Testing base.

Lets the probe be exercised end to end with no token and no network. The field
lists are transcribed from the real base on 2026-09-06, so a diff against
contract §6 here is the diff we expect to see for real.

It was previously shaped like the Airtable team's v2 guide (28 fields). That
stopped being useful once the base moved past the guide twice - once when they
shipped all five Test Type options without telling us, and once when we applied
the 14 fields.
"""

def _sel(name, choices):
    return {"id": "fld" + name.replace(" ", "")[:14], "name": name,
            "type": "singleSelect",
            "options": {"choices": [{"id": "sel%d" % i, "name": c}
                                    for i, c in enumerate(choices)]}}


def _f(name, ftype="singleLineText"):
    return {"id": "fld" + name.replace(" ", "")[:14], "name": name, "type": ftype}


# Transcribed from the live **Testing** base, 2026-09-06, after the 14 fields
# were applied - not from any guide. Regenerate from
# `ifet-firmware/docs/labos-airtable/evidence/testing-base-changes-2026-09-06/after-*.json`
# whenever the base changes.
#
# Three things this fixture used to encode are now CLOSED, and the closures are
# visible in the data below rather than in a comment:
#   - Test Type offered only "Static Load" (§10.17). All five options are live.
#   - Test Date was a `date` (§10.20). It is a `dateTime`.
#   - Corrects Attempt ID / Correction Reason were absent (§10.14). Both exist.
#
# Two things it encodes that are still true and still surprising:
#   - "Abborted" is misspelt in their base (§10.18), and LabOS sends their
#     spelling verbatim because a single-select will not accept ours.
#   - `Unit`, `Deflection Unit` and `Testing Continued` are plain text, not
#     selects, so Airtable will accept any string. Our vocabulary is enforced by
#     us before sending, never by the field type.
RAW_DATA_FIELDS = [
    _f('LabOS Attempt ID'),
    _f('LabOS Test ID'),
    _f('Attempt Number', 'number'),
    _f('Schema Version'),
    _f('Airtable Project ID'),
    _f('Airtable Mockup ID'),
    _f('Airtable Protocol ID'),
    _f('Airtable Section ID'),
    _sel('Test Type', ['Static Load', 'Cycles', 'Impact', 'Forced Entry', 'ANSI Z97.1']),
    _sel('Test Status', ['Not Started', 'In Progress', 'Completed', 'Abborted']),
    _sel('Test Result', ['Pending', 'Passed', 'Failed', 'Not Applicable', 'Inconclusive']),
    _f('Test Date', 'dateTime'),
    _f('Operator Name'),
    _f('Retest Required', 'checkbox'),
    _f('Testing Continued'),
    _f('Measured Value', 'number'),
    _f('Unit'),
    _f('Max Pressure Achieved', 'number'),
    _f('Deflection Value', 'number'),
    _f('Deflection Unit'),
    _f('Correction Reason', 'multilineText'),
    _f('Impact Result'),
    # Applied to the live Testing Base 2026-09-08. The fake base has to carry it
    # too, or the probe's "contract expects a field the base does not have"
    # check fires on a field the real base does have.
    _f('Impact Number', 'number'),
    _f('Complete LabOS JSON Response', 'multilineText'),
    _f('Notes', 'multilineText'),
    _f('Photos', 'url'),
    _f('Excel File Link', 'url'),
    _f('LabOS Report Link', 'url'),
    _f('LabOS Created At', 'dateTime'),
    _f('LabOS Updated At', 'dateTime'),
    _f('Raw Modified Time', 'lastModifiedTime'),
    _f('Corrects Attempt ID'),
    _f('LabOS Verdict By'),
    _f('LabOS Verdict At', 'dateTime'),
    _f('Testing Start Date', 'dateTime'),
    _f('Testing End Date', 'dateTime'),
    _f('LabOS Photos', 'multipleAttachments'),
]

# The real Protocol Sections table, including the 8 typed requirement fields.
# LabOS reads only Requirement Code, Applicability, Section Name and the link
# (decision A9) - the rest exist and are deliberately not consumed.
PROTOCOL_SECTION_FIELDS = [
    _f('Section Name'),
    _f('Value'),
    _sel('Status', ['Not Started', 'In Progress', 'Completed', 'Aborted']),
    _sel('Result', ['Passed', 'Failed', 'Pending', 'Not Applicable', 'Inconclusive']),
    _f('IFET Job Number', 'multipleRecordLinks'),
    _f('IFET job number (from IFET Job Number)', 'multipleLookupValues'),
    _f('Mock-Up', 'multipleLookupValues'),
    _f('Test Protocol', 'multipleRecordLinks'),
    _f('Protocol Name (from Test Protocol)', 'multipleLookupValues'),
    _f('Latest LabOS Attempt Number'),
    _f('LabOS Attempt ID'),
    _f('LabOS Retest Required', 'checkbox'),
    _f('Excel File Link', 'url'),
    _f('Notes', 'multilineText'),
    _f('Testing Date', 'date'),
    _f('LabOS Report Link', 'url'),
    _sel('Requirement Code', ['STATIC_PRESSURE', 'CYCLIC_PRESSURE', 'IMPACT_LMI', 'IMPACT_SMI', 'FORCED_ENTRY', 'ANSI_IMPACT', 'GAUGE_COUNT', 'STATIC_PROGRAMME', 'WATER_PRESSURE']),
    _sel('Requirement Kind', ['Magnitude', 'Directional Pair', 'Count', 'Enum', 'Not Applicable']),
    _sel('Applicability', ['Required', 'Not Required', 'Unconfirmed']),
    _f('Required Value', 'number'),
    _f('Required Value Inward', 'number'),
    _f('Required Value Outward', 'number'),
    _sel('Required Unit', ['PSF', 'in', 's', 'cycles', 'impacts']),
    _f('Required Option'),
]

# The pre-2026-09-06 shape, kept so a test can still exercise the "requirements
# are prose" path that §10.3 was about.
PROTOCOL_SECTION_FIELDS_FREE_TEXT = [
    _f("Section Name"), _f("Test Name"),
    _f("Requirements", "multilineText"),
    _f("IFET Project", "multipleRecordLinks"),
]


def schema(raw_fields=None, section_fields=None):
    return {"tables": [
        {"id": "tblLYcRC7q6Srjfk3", "name": "IFET Projects", "fields": [_f("IFET job number")]},
        {"id": "tblcrGv0WJn6FTTGO", "name": "Mock-Ups/Specimens", "fields": [_f("Name")]},
        {"id": "tblutO1Q8TNC4BLk0", "name": "Tests Protocols", "fields": [_f("Name")]},
        {"id": "tblqpvuJlSdkeS9PS", "name": "Protocol Sections",
         "fields": PROTOCOL_SECTION_FIELDS if section_fields is None else section_fields},
        {"id": "tblnc9SsbXU0C0FWh", "name": "LabOS Raw Data Table",
         "fields": RAW_DATA_FIELDS if raw_fields is None else raw_fields},
    ]}
