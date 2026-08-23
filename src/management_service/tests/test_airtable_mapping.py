"""P1 / Ref 47 — the ORM attempt -> envelope mapping.

Deliberately stub-based rather than SQLAlchemy-based. `mapping.py` only ever
reads attributes, so a stub exercises every path, and the suite keeps the
property the rest of these tests have: no dependencies, no database, no network,
runnable anywhere. The migration itself is rehearsed separately against a real
engine by `tests/rehearse_p1_migration.py`.
"""

import datetime as dt
import unittest

UTC = dt.timezone.utc

from app.airtable import contract as C
from app.airtable import envelope
from app.airtable.envelope import EnvelopeError
from app.airtable.mapping import envelope_values, is_syncable, owning_test


class Stub:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def project(**over):
    d = dict(airtable_project_id="recPROJ", airtable_mockup_id="recMOCK",
             airtable_mockup_name="Sliding Glass Door - 1")
    d.update(over)
    return Stub(**d)


def static_test(**over):
    d = dict(airtable_protocol_id="recPROTO", airtable_section_id="recSECT",
             airtable_section_name="DP (+) (PSF)", project=project())
    d.update(over)
    return Stub(**d)


def attempt(**over):
    """A Completed static-load attempt with everything §5.1 requires."""
    d = dict(
        static_test=static_test(), cyclic_test=None,
        labos_attempt_id="a-uuid-1", labos_test_id="t-uuid-1", trial_number=1,
        schema_version=None, corrects_attempt_id=None, correction_reason=None,
        status="Completed", test_type="Static Load", test_name=None,
        test_result="Pass", abort_reason=None,
        # §4.5 requires this on any terminal write — the envelope enforces it.
        testing_continued="Stopped",
        terminal_at=dt.datetime(2026, 8, 23, 14, 33, tzinfo=UTC), retest_required=False,
        measured_value=60.0, unit="PSF", max_pressure_achieved=63.2,
        deflection_value=None, deflection_unit="in", impact_result=None,
        cycles_required=None, cycles_completed=None,
        required_value=60.0, required_unit="PSF",
        testing_start_date=dt.datetime(2026, 8, 23, 14, 3, tzinfo=UTC),
        testing_end_date=dt.datetime(2026, 8, 23, 14, 33, tzinfo=UTC),
        operator_name="Hammad", note="no visible damage",
        photo_links=None, excel_file_link=None, report_link=None,
        labos_created_at=dt.datetime(2026, 8, 23, 14, 3, tzinfo=UTC),
        labos_updated_at=dt.datetime(2026, 8, 23, 14, 33, tzinfo=UTC),
        test_rig="System 1", labos_version="dev@2afcca5", result_rationale=None,
        # §5.1 requires a deflection reading and the JSON detail block for a
        # completed Static Load, so the default stub is a genuinely valid
        # attempt rather than one the envelope would reject.
        result_detail={"static": {"hold_time_s": 30}}, required_params=None,
        airtable_sync_state="Pending", airtable_synced_at=None,
        deflections=[Stub(max_deflection=0.31)],
    )
    d.update(over)
    return Stub(**d)


class Linkage(unittest.TestCase):
    def test_all_four_airtable_ids_are_carried(self):
        v = envelope_values(attempt())
        self.assertEqual(v["Airtable Project ID"], "recPROJ")
        self.assertEqual(v["Airtable Mock-Up ID"], "recMOCK")
        self.assertEqual(v["Airtable Protocol ID"], "recPROTO")
        self.assertEqual(v["Airtable Section ID"], "recSECT")

    def test_missing_linkage_is_refused_not_partially_sent(self):
        """A row without linkage attaches to nothing on their side."""
        a = attempt(static_test=static_test(airtable_section_id=None))
        with self.assertRaises(EnvelopeError) as ctx:
            envelope_values(a)
        self.assertIn("Airtable Section ID", str(ctx.exception))

    def test_non_strict_allows_inspecting_a_draft(self):
        a = attempt(static_test=static_test(airtable_section_id=None))
        v = envelope_values(a, strict=False)
        self.assertNotIn("Airtable Section ID", v)

    def test_owning_test_finds_either_subclass(self):
        cyclic = static_test(airtable_section_name="Cyclic (PSF)")
        a = attempt(static_test=None, cyclic_test=cyclic)
        self.assertIs(owning_test(a), cyclic)


class Timestamps(unittest.TestCase):
    def test_naive_datetimes_are_refused_by_the_envelope(self):
        """Why the columns are TIMESTAMP WITH TIME ZONE: a naive value has to be
        rejected somewhere, and the storage layer would guess instead."""
        a = attempt(testing_start_date=dt.datetime(2026, 8, 23, 14, 3))
        with self.assertRaises(EnvelopeError) as ctx:
            envelope.build(envelope_values(a), status=a.status)
        self.assertIn("naive datetime", str(ctx.exception))


class Fields(unittest.TestCase):
    def test_trial_number_is_the_attempt_number(self):
        self.assertEqual(envelope_values(attempt(trial_number=3))["Attempt Number"], 3)

    def test_schema_version_defaults_to_the_contract(self):
        self.assertEqual(envelope_values(attempt())["Schema Version"], C.CONTRACT_VERSION)

    def test_test_name_falls_back_to_the_airtable_section_name(self):
        self.assertEqual(envelope_values(attempt())["Test Name"], "DP (+) (PSF)")

    def test_none_values_are_omitted_not_blanked(self):
        """Contract §5 — a key with no value is absent, never an empty string."""
        v = envelope_values(attempt())
        self.assertNotIn("Abort Reason", v)
        self.assertNotIn("Correction Reason", v)

    def test_retest_required_is_always_an_explicit_bool(self):
        """§4.5 — an omitted value must not be readable as false."""
        for given in (None, False, 0):
            v = envelope_values(attempt(retest_required=given))
            self.assertIs(v["Retest Required"], False)
        self.assertIs(envelope_values(attempt(retest_required=True))["Retest Required"], True)

    def test_requirement_the_attempt_ran_against_is_carried(self):
        """§10.19 traceability — this is what makes a wrong requirement findable."""
        v = envelope_values(attempt())
        self.assertEqual(v["Required Value"], 60.0)
        self.assertEqual(v["Required Unit"], "PSF")


class DeflectionDerivation(unittest.TestCase):
    def test_explicit_column_wins(self):
        a = attempt(deflection_value=0.31,
                    deflections=[Stub(max_deflection=0.9)])
        self.assertEqual(envelope_values(a)["Deflection Value"], 0.31)

    def test_falls_back_to_the_largest_gauge_reading(self):
        a = attempt(deflection_value=None,
                    deflections=[Stub(max_deflection=0.21),
                                 Stub(max_deflection=0.44),
                                 Stub(max_deflection=0.08)])
        self.assertEqual(envelope_values(a)["Deflection Value"], 0.44)

    def test_no_gauges_means_the_key_is_absent(self):
        a = attempt(deflection_value=None, deflections=[])
        self.assertNotIn("Deflection Value", envelope_values(a))


class EndToEnd(unittest.TestCase):
    """The point of P1: a stored attempt must produce a contract-valid payload."""

    def test_completed_attempt_builds_a_valid_wire_payload(self):
        a = attempt()
        wire = envelope.build(envelope_values(a), status=a.status)
        self.assertEqual(wire["LabOS Attempt ID"], "a-uuid-1")
        self.assertEqual(wire["Airtable Mockup ID"], "recMOCK")   # their spelling
        self.assertEqual(wire["Test Result"], "Passed")           # §10.16 translation
        self.assertEqual(wire["Attempt Number"], 1)

    def test_aborted_attempt_carries_their_misspelling(self):
        a = attempt(status="Aborted", test_result=None,
                    abort_reason="Equipment Fault")
        wire = envelope.build(envelope_values(a), status=a.status)
        self.assertEqual(wire["Test Status"], "Abborted")         # §10.18

    def test_in_progress_attempt_omits_the_result(self):
        a = attempt(status="In Progress", test_result=None, testing_end_date=None)
        wire = envelope.build(envelope_values(a), status=a.status)
        self.assertNotIn("Test Result", wire)


class SyncEligibility(unittest.TestCase):
    def test_pre_integration_attempts_are_never_syncable(self):
        """The 623 historical rows must not be uploaded on first run."""
        self.assertFalse(is_syncable(attempt(airtable_sync_state="Excluded")))

    def test_a_terminal_synced_attempt_is_never_rewritten(self):
        """§3 — once terminal and written, that attempt ID is final."""
        a = attempt(terminal_at=dt.datetime(2026, 8, 23, 14, 33, tzinfo=UTC),
                    airtable_synced_at=dt.datetime(2026, 8, 23, 14, 34, tzinfo=UTC))
        self.assertFalse(is_syncable(a))

    def test_a_terminal_but_unsynced_attempt_still_needs_writing(self):
        a = attempt(terminal_at=dt.datetime(2026, 8, 23, 14, 33, tzinfo=UTC),
                    airtable_synced_at=None)
        self.assertTrue(is_syncable(a))

    def test_an_in_progress_attempt_is_syncable(self):
        self.assertTrue(is_syncable(attempt(status="In Progress", terminal_at=None)))


if __name__ == "__main__":
    unittest.main()
