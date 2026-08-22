"""Envelope tests — the payload contract, offline.

No network, no token. These assert the wire form is exactly what write contract
v0.3 specifies, because every one of these mistakes produces a row that looks
plausible in Airtable and is wrong.
"""

import datetime as dt
import json
import unittest

from app.airtable import contract as C
from app.airtable.envelope import (
    EnvelopeError,
    build,
    build_start,
    build_terminal,
    options_from_snapshot,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 8, 22, 14, 3, 0, tzinfo=UTC)
T1 = dt.datetime(2026, 8, 22, 14, 33, 20, tzinfo=UTC)


def base(**over):
    """The always-required set, plus enough to be a valid Static Load start."""
    v = {
        "Airtable Project ID": "recProject123",
        "Airtable Mock-Up ID": "recMockup123",
        "Airtable Protocol ID": "recProtocol123",
        "Airtable Section ID": "recSection123",
        "LabOS Test ID": "TEST-2026-000897",
        "LabOS Attempt ID": "ATT-2026-000145",
        "Attempt Number": 1,
        "Test Type": C.STATIC_LOAD,
        "Test Name": "Static Air Pressure — Uniform Load Deflection",
        "Testing Start Date": T0,
        "LabOS Created At": T0,
        "LabOS Updated At": T0,
    }
    v.update(over)
    return v


def completed(**over):
    v = base(
        Operator_Name=None)  # placeholder removed below
    v.pop("Operator_Name", None)
    v.update({
        "Operator Name": "Technician Name",
        "Retest Required": False,
        "Testing Continued": "Stopped",
        "Testing End Date": T1,
        "Test Result": "Pass",
        "Measured Value": 40.0,
        "Unit": "PSF",
        "Max Pressure Achieved": 41.0,
        "Deflection Value": 0.42,
        "Deflection Unit": "in",
        "Required Value": 40.0,
        "Required Unit": "PSF",
        "Result Detail (JSON)": {"steps": [{"step": 1, "target": 20.0}]},
    })
    v.update(over)
    return v


def detail(wire):
    return json.loads(wire["Complete LabOS JSON Response"])


class WireNames(unittest.TestCase):
    def test_uses_their_spelling_not_ours(self):
        w = build_start(base())
        self.assertIn("Airtable Mockup ID", w)
        self.assertNotIn("Airtable Mock-Up ID", w)

    def test_report_link_and_json_field_renamed(self):
        w = build_terminal(completed(**{"Report Link": "https://labos.example/r/1.pdf"}))
        self.assertIn("LabOS Report Link", w)
        self.assertNotIn("Report Link", w)
        self.assertIn("Complete LabOS JSON Response", w)
        self.assertNotIn("Result Detail (JSON)", w)

    def test_every_emitted_key_exists_in_the_v2_base(self):
        """Nothing is sent that the Airtable team has not created."""
        w = build_terminal(completed())
        for key in w:
            self.assertIn(key, C.EXPECTED_LIVE, f"{key!r} is not a live Airtable field")


class BlankRules(unittest.TestCase):
    def test_none_omits_the_key(self):
        w = build_start(base(Notes=None))
        self.assertNotIn("Notes", w)

    def test_zero_is_data_not_a_blank(self):
        w = build_terminal(completed(**{"Measured Value": 0, "Max Pressure Achieved": 0}))
        self.assertEqual(w["Measured Value"], 0)
        self.assertEqual(w["Max Pressure Achieved"], 0)

    def test_false_is_data(self):
        w = build_terminal(completed(**{"Retest Required": False}))
        self.assertIs(w["Retest Required"], False)

    def test_empty_string_is_refused(self):
        with self.assertRaises(EnvelopeError) as ctx:
            build_start(base(Notes=""))
        self.assertIn("sentinel", str(ctx.exception))

    def test_na_sentinels_are_refused(self):
        for junk in ("N/A", "-", "Not Available"):
            with self.assertRaises(EnvelopeError):
                build_start(base(Notes=junk))

    def test_unknown_field_is_refused(self):
        with self.assertRaises(EnvelopeError) as ctx:
            build_start(base(**{"Wingspan": 3}))
        self.assertIn("not in write contract", str(ctx.exception))


class Lifecycle(unittest.TestCase):
    def test_start_write_sets_in_progress(self):
        self.assertEqual(build_start(base())["Test Status"], "In Progress")

    def test_start_write_must_not_carry_a_result(self):
        with self.assertRaises(EnvelopeError):
            build_start(base(**{"Test Result": "Pass"}))

    def test_terminal_requires_operator_and_disposition(self):
        v = completed()
        v.pop("Operator Name")
        with self.assertRaises(EnvelopeError) as ctx:
            build_terminal(v)
        self.assertIn("Operator Name", str(ctx.exception))

    def test_completed_requires_a_result(self):
        v = completed()
        v.pop("Test Result")
        with self.assertRaises(EnvelopeError):
            build_terminal(v)

    def test_aborted_requires_a_reason(self):
        v = completed()
        v.pop("Test Result")
        with self.assertRaises(EnvelopeError) as ctx:
            build_terminal(v, status=C.ABORTED)
        self.assertIn("Abort Reason", str(ctx.exception))

    def test_aborted_with_a_reason_is_accepted(self):
        v = completed(**{"Abort Reason": "Equipment Fault"})
        v.pop("Test Result")
        w = build_terminal(v, status=C.ABORTED)
        self.assertEqual(w["Test Status"], "Aborted")
        # No column for it yet, so it must survive in the JSON valve.
        self.assertEqual(detail(w)["labos_extra"]["abort_reason"], "Equipment Fault")

    def test_both_writes_share_the_merge_key(self):
        s, t = build_start(base()), build_terminal(completed())
        self.assertEqual(s["LabOS Attempt ID"], t["LabOS Attempt ID"])

    def test_always_required_set_is_enforced(self):
        v = base()
        v.pop("LabOS Attempt ID")
        with self.assertRaises(EnvelopeError) as ctx:
            build_start(v)
        self.assertIn("LabOS Attempt ID", str(ctx.exception))


class TestTypeMatrix(unittest.TestCase):
    def test_static_load_needs_its_measurements(self):
        v = completed()
        v.pop("Max Pressure Achieved")
        with self.assertRaises(EnvelopeError) as ctx:
            build_terminal(v)
        self.assertIn("Max Pressure Achieved", str(ctx.exception))
        self.assertIn("§5.1", str(ctx.exception))

    def test_cycles_needs_cycle_counts(self):
        v = completed(**{"Test Type": C.CYCLES})
        with self.assertRaises(EnvelopeError) as ctx:
            build_terminal(v)
        self.assertIn("Cycles Required", str(ctx.exception))

    def test_cycles_passes_with_counts(self):
        v = completed(**{"Test Type": C.CYCLES,
                         "Cycles Required": 4500, "Cycles Completed": 4500})
        w = build_terminal(v)
        self.assertEqual(detail(w)["labos_extra"]["cycles_completed"], 4500)

    def test_impact_needs_impact_result_and_photos(self):
        v = completed(**{"Test Type": C.IMPACT})
        for k in ("Measured Value", "Unit", "Max Pressure Achieved",
                  "Deflection Value", "Deflection Unit", "Required Value", "Required Unit"):
            v.pop(k, None)
        with self.assertRaises(EnvelopeError) as ctx:
            build_terminal(v)
        self.assertIn("Impact Result", str(ctx.exception))

    def test_unknown_test_type_refused(self):
        with self.assertRaises(EnvelopeError):
            build_start(base(**{"Test Type": "Hurricane"}))


class PairwiseRules(unittest.TestCase):
    def test_measured_value_requires_unit(self):
        v = completed()
        v.pop("Unit")
        with self.assertRaises(EnvelopeError) as ctx:
            build_terminal(v)
        self.assertIn("Unit", str(ctx.exception))

    def test_deflection_value_requires_deflection_unit(self):
        v = completed()
        v.pop("Deflection Unit")
        with self.assertRaises(EnvelopeError):
            build_terminal(v)

    def test_correction_reason_without_reference_refused(self):
        with self.assertRaises(EnvelopeError):
            build_terminal(completed(**{"Correction Reason": "unit mix-up"}))


class SelectOptions(unittest.TestCase):
    def test_invalid_option_is_a_contract_error_not_a_coercion(self):
        with self.assertRaises(EnvelopeError) as ctx:
            build_terminal(completed(**{"Unit": "furlongs"}))
        self.assertIn("never invents a select option", str(ctx.exception))

    def test_contract_spelling_accepted_by_default(self):
        self.assertEqual(build_terminal(completed())["Test Result"], "Pass")

    def test_live_options_override_the_contract(self):
        """§10.16 — their base says 'Passed'. The live set is authoritative."""
        live = {"Test Result": ("Passed", "Failed", "Inconclusive")}
        with self.assertRaises(EnvelopeError):
            build_terminal(completed(), live_options=live)          # 'Pass' now invalid
        w = build_terminal(completed(**{"Test Result": "Passed"}), live_options=live)
        self.assertEqual(w["Test Result"], "Passed")

    def test_options_from_snapshot_feeds_the_builder(self):
        from app.airtable import probe
        from tests import fake_schema as FS
        live = options_from_snapshot(probe.build_snapshot(FS.schema()))
        self.assertEqual(live["Test Result"], ("Passed", "Failed", "Inconclusive"))
        w = build_terminal(completed(**{"Test Result": "Passed"}), live_options=live)
        self.assertEqual(w["Test Result"], "Passed")


class TestDateCollapse(unittest.TestCase):
    """§10.13 — one date field where the contract wants two."""

    def test_start_instant_goes_into_their_test_date(self):
        w = build_start(base())
        self.assertEqual(w["Test Date"], "2026-08-22T14:03:00Z")

    def test_end_time_is_preserved_in_the_json_valve(self):
        d = detail(build_terminal(completed()))["labos_extra"]
        self.assertEqual(d["testing_end_date"], "2026-08-22T14:33:20Z")

    def test_duration_is_computed_rather_than_lost(self):
        d = detail(build_terminal(completed()))["labos_extra"]
        self.assertEqual(d["duration_s"], 1820)

    def test_terminal_write_does_not_move_test_date_off_the_start(self):
        self.assertEqual(build_terminal(completed())["Test Date"],
                         build_start(base())["Test Date"])

    def test_naive_datetime_refused(self):
        with self.assertRaises(EnvelopeError) as ctx:
            build_start(base(**{"Testing Start Date": dt.datetime(2026, 8, 22, 14, 3)}))
        self.assertIn("naive datetime", str(ctx.exception))


class CorrectionGuard(unittest.TestCase):
    """§3.1 / §10.14 — the field does not exist yet, and this matters."""

    def test_correction_refused_while_the_field_is_absent(self):
        v = completed(**{"Corrects Attempt ID": "ATT-2026-000144",
                         "Correction Reason": "PSI logged as PSF",
                         "Attempt Number": 2})
        with self.assertRaises(EnvelopeError) as ctx:
            build_terminal(v)
        msg = str(ctx.exception)
        self.assertIn("§10.14", msg)
        self.assertIn("retest", msg)

    def test_override_records_it_in_the_json_valve(self):
        v = completed(**{"Corrects Attempt ID": "ATT-2026-000144",
                         "Correction Reason": "PSI logged as PSF",
                         "Attempt Number": 2})
        w = build_terminal(v, allow_unreferenced_correction=True)
        extra = detail(w)["labos_extra"]
        self.assertEqual(extra["corrects_attempt_id"], "ATT-2026-000144")
        self.assertIn("PSI", extra["correction_reason"])

    def test_reference_without_a_reason_refused(self):
        v = completed(**{"Corrects Attempt ID": "ATT-2026-000144"})
        with self.assertRaises(EnvelopeError):
            build_terminal(v, allow_unreferenced_correction=True)


class JsonValve(unittest.TestCase):
    def test_carries_schema_and_test_type(self):
        d = detail(build_terminal(completed()))
        self.assertEqual(d["schema"], C.CONTRACT_VERSION)
        self.assertEqual(d["test_type"], C.STATIC_LOAD)

    def test_callers_detail_survives_alongside_the_overflow(self):
        d = detail(build_terminal(completed()))
        self.assertEqual(d["steps"], [{"step": 1, "target": 20.0}])
        self.assertIn("labos_extra", d)

    def test_absent_fields_are_never_silently_dropped(self):
        w = build_terminal(completed(**{"Test Rig": "System 2",
                                        "Result Rationale": "0.42 in <= 0.55 in"}))
        extra = detail(w)["labos_extra"]
        self.assertEqual(extra["test_rig"], "System 2")
        self.assertIn("0.42", extra["result_rationale"])
        self.assertNotIn("Test Rig", w)

    def test_required_snapshot_values_ride_in_the_valve(self):
        extra = detail(build_terminal(completed()))["labos_extra"]
        self.assertEqual(extra["required_value"], 40.0)
        self.assertEqual(extra["required_unit"], "PSF")

    def test_field_is_valid_json(self):
        json.loads(build_terminal(completed())["Complete LabOS JSON Response"])

    def test_string_detail_must_be_valid_json(self):
        with self.assertRaises(EnvelopeError):
            build_terminal(completed(**{"Result Detail (JSON)": "not json"}))


class EndToEnd(unittest.TestCase):
    """Envelope output must be acceptable to the client without adaptation."""

    def test_payload_reaches_airtable_through_the_allowlisted_table(self):
        from app.airtable.client import AirtableClient
        from app.config import AirtableSettings, BASE_TESTING, TABLE_RAW_DATA
        from tests.test_airtable_client import Transport, TOKEN

        t = Transport((200, {"records": []}))
        c = AirtableClient(
            AirtableSettings({"AIRTABLE_TOKEN": TOKEN, "AIRTABLE_BASE_ID": BASE_TESTING}),
            transport=t, sleep=lambda _s: None)
        c.upsert_records(TABLE_RAW_DATA, [build_terminal(completed())])

        body = t.calls[0]["body"]
        self.assertEqual(body["performUpsert"]["fieldsToMergeOn"], ["LabOS Attempt ID"])
        fields = body["records"][0]["fields"]
        self.assertEqual(fields["Airtable Mockup ID"], "recMockup123")
        self.assertEqual(fields["Test Status"], "Completed")
        self.assertNotIn("Testing End Date", fields)


if __name__ == "__main__":
    unittest.main()


class ContractAlignment(unittest.TestCase):
    """Guards against the code and the prose contract drifting apart.

    The failure this prevents is quiet: a field marked R in contract §4 that no
    validation actually enforces looks fine in review and lets an incomplete
    attempt reach Airtable.
    """

    def test_every_R_field_is_enforced_somewhere(self):
        enforced = set(C.ALWAYS_REQUIRED) | set(C.TERMINAL_REQUIRED)
        unenforced = [f.labos_name for f in C.FIELDS
                      if f.req == C.REQUIRED and f.labos_name not in enforced]
        self.assertEqual(unenforced, [],
                         "fields marked R in contract §4 with no validation")

    def test_required_sets_reference_real_fields(self):
        for name in C.ALWAYS_REQUIRED + C.TERMINAL_REQUIRED:
            self.assertIn(name, C.BY_LABOS_NAME, f"{name!r} is not a contract §4 field")

    def test_matrix_references_real_fields(self):
        for test_type, names in C.REQUIRED_BY_TEST_TYPE.items():
            self.assertIn(test_type, C.TEST_TYPES)
            for name in names:
                self.assertIn(name, C.BY_LABOS_NAME)

    def test_test_name_is_required_even_though_the_column_does_not_exist(self):
        v = base()
        v.pop("Test Name")
        with self.assertRaises(EnvelopeError) as ctx:
            build_start(v)
        self.assertIn("Test Name", str(ctx.exception))

    def test_test_name_travels_in_the_json_valve(self):
        w = build_start(base())
        self.assertNotIn("Test Name", w)
        self.assertIn("Static Air Pressure", detail(w)["labos_extra"]["test_name"])

    def test_testing_end_date_is_required_on_a_terminal_write(self):
        v = completed()
        v.pop("Testing End Date")
        with self.assertRaises(EnvelopeError) as ctx:
            build_terminal(v)
        self.assertIn("Testing End Date", str(ctx.exception))

    def test_start_write_does_not_require_an_end_date(self):
        build_start(base())          # must not raise
