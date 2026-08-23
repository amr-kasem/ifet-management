"""ORM attempt -> write-contract envelope (P1 / Ref 47).

This is the seam between the LabOS database and `envelope.build()`. It exists as
its own module, rather than as a method on the model, for one reason: the
mapping is a **statement about the contract**, and it should be reviewable
against contract §4 side by side without reading SQLAlchemy.

It deliberately does no validation. Every rule — which fields are required for
which test type, blank handling, option spelling, the JSON overflow — already
lives in `envelope.build()`, and duplicating any of it here would create a
second place for the contract to be wrong.

    values = envelope_values(attempt)
    payload = envelope.build(values, status=attempt.status, live_options=...)
"""

from . import contract as C
from .envelope import EnvelopeError

# Which ORM relationship leads from an attempt back to its test, per subclass.
# Both carry the AirtableProtocolRef mixin, so once found they are interchangeable.
_TEST_ATTRS = ("static_test", "cyclic_test")


def owning_test(attempt):
    """The StaticTest / CyclicTest this attempt belongs to, or None."""
    for attr in _TEST_ATTRS:
        test = getattr(attempt, attr, None)
        if test is not None:
            return test
    return None


def _deflection_value(attempt):
    """The deflection LabOS reports for this attempt.

    Prefer the explicit column: an operator or the pass/fail step may have
    chosen which gauge is authoritative. Fall back to the largest measured
    deflection across gauges, which is the conservative reading and the one a
    structural pass/fail turns on.
    """
    if attempt.deflection_value is not None:
        return attempt.deflection_value
    readings = [d.max_deflection for d in (attempt.deflections or [])
                if d.max_deflection is not None]
    return max(readings) if readings else None


def envelope_values(attempt, *, strict=True):
    """Build the `{LabOS field name: value}` dict for `envelope.build()`.

    `strict` raises when the Airtable linkage is missing. That linkage is what
    tells Airtable which Protocol Section a result belongs to, so a payload
    without it is not a partially-good record — it is a record that would land
    unattached to anything. Pass `strict=False` only to inspect a draft.
    """
    test = owning_test(attempt)
    project = getattr(test, "project", None) if test is not None else None

    if strict:
        missing = []
        if project is None or not project.airtable_project_id:
            missing.append("Airtable Project ID")
        if project is None or not project.airtable_mockup_id:
            missing.append("Airtable Mock-Up ID")
        if test is None or not test.airtable_protocol_id:
            missing.append("Airtable Protocol ID")
        if test is None or not test.airtable_section_id:
            missing.append("Airtable Section ID")
        if missing:
            raise EnvelopeError(
                f"attempt {attempt.labos_attempt_id!r} has no Airtable linkage for "
                f"{missing} (contract §4.1). The project or protocol has not been "
                "bound to an Airtable record yet — bind it, or exclude this "
                "attempt from sync. A row written without linkage attaches to "
                "nothing on their side."
            )

    values = {
        # -- §4.1 identity ---------------------------------------------------
        "Airtable Project ID": getattr(project, "airtable_project_id", None),
        "Airtable Mock-Up ID": getattr(project, "airtable_mockup_id", None),
        "Airtable Protocol ID": getattr(test, "airtable_protocol_id", None),
        "Airtable Section ID": getattr(test, "airtable_section_id", None),
        "LabOS Test ID": attempt.labos_test_id,
        "LabOS Attempt ID": attempt.labos_attempt_id,
        "Attempt Number": attempt.trial_number,
        "Schema Version": attempt.schema_version or C.CONTRACT_VERSION,
        "Corrects Attempt ID": attempt.corrects_attempt_id,
        "Correction Reason": attempt.correction_reason,

        # -- §4.3 descriptors -------------------------------------------------
        # Test Name falls back to the Airtable Section Name, which is what a
        # human reading their base would recognise the row by.
        "Test Name": attempt.test_name or getattr(test, "airtable_section_name", None),
        "Test Type": attempt.test_type,
        "Test Result": attempt.test_result,
        "Abort Reason": attempt.abort_reason,

        # -- §4.4 measurements -------------------------------------------------
        "Measured Value": attempt.measured_value,
        "Unit": attempt.unit,
        "Max Pressure Achieved": attempt.max_pressure_achieved,
        "Deflection Value": _deflection_value(attempt),
        "Deflection Unit": attempt.deflection_unit,
        "Impact Result": attempt.impact_result,
        "Required Value": attempt.required_value,
        "Required Unit": attempt.required_unit,
        "Cycles Required": attempt.cycles_required,
        "Cycles Completed": attempt.cycles_completed,
        "Result Detail (JSON)": attempt.result_detail,

        # -- §4.5 timing, people, disposition ---------------------------------
        "Testing Start Date": attempt.testing_start_date,
        "Testing End Date": attempt.testing_end_date,
        "Operator Name": attempt.operator_name,
        # Explicit bool: contract §4.5 says an omitted value must not read as
        # false, so this is never allowed to become None by accident.
        "Retest Required": bool(attempt.retest_required),
        "Testing Continued": attempt.testing_continued,
        "Notes": attempt.note,

        # -- §4.6 artifacts & metadata -----------------------------------------
        "Photos": attempt.photo_links,
        "Excel File Link": attempt.excel_file_link,
        "Report Link": attempt.report_link,
        "LabOS Created At": attempt.labos_created_at,
        "LabOS Updated At": attempt.labos_updated_at,
        "Test Rig": attempt.test_rig,
        "LabOS Version": attempt.labos_version,
        "Result Rationale": attempt.result_rationale,
    }

    # §5: a key with no value is omitted entirely rather than sent blank. The
    # envelope enforces this too; dropping them here keeps the payload readable
    # in logs and makes the two layers agree rather than merely not conflict.
    return {k: v for k, v in values.items() if v is not None}


def is_syncable(attempt):
    """Whether the sync worker (W4) may write this attempt at all.

    Two independent reasons to refuse, both contract-level rather than
    operational:

    * `Excluded` — pre-integration attempts, marked by the P1 migration. Without
      this the first worker run would upload every test IFET has ever done.
    * terminal + already synced — contract §3 makes a terminal attempt final.
      Once written, LabOS never writes that attempt ID again; a correction is a
      new row, not an edit.
    """
    if attempt.airtable_sync_state == "Excluded":
        return False
    if attempt.terminal_at is not None and attempt.airtable_synced_at is not None:
        return False
    return True
