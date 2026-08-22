"""A synthetic Airtable base schema shaped like the Airtable team's v2 guide.

Lets the probe be exercised end to end with no token and no network. The field
list is exactly the 28 the v2 PDF publishes for `LabOS Raw Data Table`, so a
diff against contract §4 here is the diff we expect to see for real.
"""

def _sel(name, choices):
    return {"id": "fld" + name.replace(" ", "")[:14], "name": name,
            "type": "singleSelect",
            "options": {"choices": [{"id": "sel%d" % i, "name": c}
                                    for i, c in enumerate(choices)]}}


def _f(name, ftype="singleLineText"):
    return {"id": "fld" + name.replace(" ", "")[:14], "name": name, "type": ftype}


# The 28 fields v2 lists, in its own grouping order.
RAW_DATA_FIELDS = [
    _f("LabOS Attempt ID"), _f("LabOS Test ID"), _f("Attempt Number", "number"),
    _f("Schema Version"), _f("Airtable Project ID"), _f("Airtable Mockup ID"),
    _f("Airtable Protocol ID"), _f("Airtable Section ID"),
    _sel("Test Type", ["Static Load", "Cycles", "Impact", "Forced Entry", "ANSI Z97.1"]),
    _sel("Test Status", ["In Progress", "Completed", "Aborted"]),
    # v2's worked example sends "Passed" — this is contract §10.16 in the wild.
    _sel("Test Result", ["Passed", "Failed", "Inconclusive"]),
    _f("Test Date", "date"), _f("Operator Name"),
    _f("Retest Required", "checkbox"),
    _sel("Testing Continued", ["Continued", "Stopped"]),
    _f("Measured Value", "number"),
    _sel("Unit", ["PSF", "PSI", "in", "mm", "lbf", "N", "cycles", "s"]),
    _f("Max Pressure Achieved", "number"), _f("Deflection Value", "number"),
    _sel("Deflection Unit", ["in", "mm"]),
    _f("Impact Result"),
    _f("Notes", "multilineText"), _f("Photos", "multilineText"),
    _f("Excel File Link", "url"), _f("LabOS Report Link", "url"),
    _f("Complete LabOS JSON Response", "multilineText"),
    _f("LabOS Created At", "dateTime"), _f("LabOS Updated At", "dateTime"),
]

PROTOCOL_SECTION_FIELDS = [
    _f("Section Name"), _f("Test Name"),
    # The realistic bad case for §10.3: requirements as prose.
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
