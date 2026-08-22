"""Probe tests — run the real report logic against a v2-shaped fake base.

These assert that the probe actually catches the things it exists to catch. A
probe that prints a clean report against a broken base is worse than no probe,
so every check here has a matching negative case.
"""

import json
import os
import tempfile
import unittest

from app.airtable import contract as C
from app.airtable import probe
from tests import fake_schema as FS


def run(schema):
    rep = probe.Report()
    probe.check_tables(schema, rep)
    probe.check_raw_table(schema, rep)
    probe.check_read_side(schema, rep)
    return rep, rep.render()


class AgainstTheV2Base(unittest.TestCase):
    """The base exactly as the v2 guide describes it."""

    def setUp(self):
        self.rep, self.text = run(FS.schema())

    def test_all_five_labos_tables_verified(self):
        for table_id in ("tblLYcRC7q6Srjfk3", "tblcrGv0WJn6FTTGO", "tblutO1Q8TNC4BLk0",
                         "tblqpvuJlSdkeS9PS", "tblnc9SsbXU0C0FWh"):
            self.assertIn(table_id, self.text)
        self.assertEqual(self.rep.failures, 0, self.text)

    def test_every_field_v2_promised_is_found(self):
        self.assertIn("all 28 fields promised by v2 are present", self.text)

    def test_catches_the_Passed_vs_Pass_option_mismatch(self):
        """§10.16 — the landmine in their own worked example."""
        self.assertIn("§10.16", self.rep.answers)
        self.assertIn("Passed", self.rep.answers["§10.16"])
        self.assertIn("the client must send THEIR spelling", self.text)

    def test_reports_photos_is_not_an_attachment(self):
        self.assertIn("§10.1", self.rep.answers)
        self.assertIn("not an attachment", self.rep.answers["§10.1"])

    def test_reports_the_id_fields_are_plain_text(self):
        self.assertIn("§10.2", self.rep.answers)
        self.assertIn("all four ID fields are plain text", self.text)

    def test_reports_the_impact_result_shape(self):
        self.assertIn("§10.5", self.rep.answers)

    def test_flags_the_two_blocking_absent_fields(self):
        for name in C.BLOCKING_ABSENT:
            self.assertIn(f"{name!r} still absent", self.text)
        self.assertIn("BLOCKING", self.text)

    def test_says_plainly_that_it_cannot_close_the_read_side_item(self):
        self.assertIn("A probe CANNOT close §10.3", self.text)

    def test_read_side_free_text_is_not_mistaken_for_structure(self):
        """Protocol Sections has only a long-text 'Requirements' field."""
        self.assertIn("none of the parameter fields contract §9.1 asks for exist", self.text)


class NegativeCases(unittest.TestCase):
    def test_photos_as_attachment_is_a_failure(self):
        fields = [dict(f) for f in FS.RAW_DATA_FIELDS]
        for f in fields:
            if f["name"] == "Photos":
                f["type"] = "multipleAttachments"
        rep, text = run(FS.schema(raw_fields=fields))
        self.assertGreater(rep.failures, 0)
        self.assertIn("forbidden", text)

    def test_attempt_id_as_a_formula_field_is_a_failure(self):
        """§2 — a computed field cannot be used in fieldsToMergeOn."""
        fields = [dict(f) for f in FS.RAW_DATA_FIELDS]
        for f in fields:
            if f["name"] == "LabOS Attempt ID":
                f["type"] = "formula"
        rep, text = run(FS.schema(raw_fields=fields))
        self.assertGreater(rep.failures, 0)
        self.assertIn("LabOS Attempt ID", text)

    def test_a_missing_promised_field_is_a_failure(self):
        fields = [f for f in FS.RAW_DATA_FIELDS if f["name"] != "Max Pressure Achieved"]
        rep, text = run(FS.schema(raw_fields=fields))
        self.assertGreater(rep.failures, 0)
        self.assertIn("promised by v2 but MISSING", text)

    def test_a_missing_write_target_is_a_failure(self):
        schema = FS.schema()
        schema["tables"] = [t for t in schema["tables"] if t["id"] != "tblnc9SsbXU0C0FWh"]
        rep, text = run(schema)
        self.assertGreater(rep.failures, 0)
        self.assertIn("NOT FOUND", text)

    def test_link_to_record_id_fields_are_flagged(self):
        fields = [dict(f) for f in FS.RAW_DATA_FIELDS]
        for f in fields:
            if f["name"] == "Airtable Section ID":
                f["type"] = "multipleRecordLinks"
        rep, text = run(FS.schema(raw_fields=fields))
        self.assertIn("link-to-record, not text", text)

    def test_correction_fields_being_added_is_reported_as_a_close(self):
        fields = list(FS.RAW_DATA_FIELDS) + [
            FS._f("Corrects Attempt ID"), FS._f("Correction Reason", "multilineText")]
        rep, text = run(FS.schema(raw_fields=fields))
        self.assertIn("§10.14 can close", text)

    def test_machine_readable_read_side_is_recognised(self):
        sections = [FS._f("Design Pressure Inward (PSF)", "number"),
                    FS._f("Hold Time (s)", "number")]
        rep, text = run(FS.schema(section_fields=sections))
        self.assertIn("machine-readable parameter present", text)


class Snapshot(unittest.TestCase):
    def test_snapshot_captures_field_ids_for_contract_9_binding(self):
        snap = probe.build_snapshot(FS.schema())
        raw = snap["tables"]["tblnc9SsbXU0C0FWh"]
        self.assertEqual(raw["name"], "LabOS Raw Data Table")
        self.assertIn("LabOS Attempt ID", raw["fields"])
        self.assertTrue(raw["fields"]["LabOS Attempt ID"]["id"].startswith("fld"))
        self.assertEqual(snap["contract_version"], C.CONTRACT_VERSION)

    def test_snapshot_is_json_serialisable_and_stable(self):
        snap = probe.build_snapshot(FS.schema())
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snap.json")
            with open(path, "w") as fh:
                json.dump(snap, fh, indent=2, sort_keys=True)
            with open(path) as fh:
                self.assertEqual(json.load(fh), snap)


class ContractDataIntegrity(unittest.TestCase):
    def test_wire_names_are_unique(self):
        names = [f.wire_name for f in C.FIELDS]
        self.assertEqual(len(names), len(set(names)))

    def test_expected_live_matches_the_28_fields_v2_publishes(self):
        self.assertEqual(len(C.EXPECTED_LIVE), len(FS.RAW_DATA_FIELDS))
        self.assertEqual(set(C.EXPECTED_LIVE), {f["name"] for f in FS.RAW_DATA_FIELDS})

    def test_blocking_absent_are_genuinely_marked_absent(self):
        for name in C.BLOCKING_ABSENT:
            self.assertEqual(C.BY_WIRE_NAME[name].v2, C.ABSENT)


if __name__ == "__main__":
    unittest.main()
