"""The inbound half: mirror, selection, validation, import, pre-fill, freeze.

Requirements entered in Airtable have to reach a LabOS project with its
parameters filled in, and they have to do it **through the same create path a
typed project uses**. These tests are written around the properties that make
that safe rather than merely working:

* the mirror copies only allowlisted fields, and **refuses** anything else;
* `Value` is never read — contract §10.19, the extractor's shifted column;
* a section whose unit and kind disagree is refused, not best-guessed;
* a repeated import returns the project it already made;
* pre-filled and typed projects go through one function, so the fourteen derived
  stages cannot drift between them;
* the requirement is frozen when an attempt starts, so a later upstream edit
  cannot change what a finished test claims to have been run against.
"""

import os
import unittest

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

try:
    from fastapi.testclient import TestClient
except ImportError:                                          # pragma: no cover
    TestClient = None

PROJ, SPEC, PROT = "recJOB0000000001", "recMOCK000000001", "recPROT000000001"


def _client_and_session():
    from app import main
    from app.data.models import Base

    url = os.environ.get("M2_DATABASE_URL", "sqlite://")
    kw = {}
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        kw = {"connect_args": {"check_same_thread": False},
              "poolclass": StaticPool}
    engine = sa.create_engine(url, **kw)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = _get_db
    return TestClient(main.app), Session


def section(record_id, code, **over):
    """A mirrored section, defaulted to something valid for its code."""
    from app.airtable import requirements as req
    from app.airtable.mirror import AtMirrorSection
    kind = req.KIND_BY_CODE[code]
    base = {"record_id": record_id, "protocol_record_id": PROT,
            "section_name": code, "requirement_code": code,
            "requirement_kind": kind, "applicability": "Required"}
    if kind == "Directional Pair":
        base.update(required_value_inward=60.0, required_value_outward=45.0,
                    required_unit="PSF")
    elif code in ("IMPACT_LMI", "IMPACT_SMI"):
        base.update(required_value=3.0, required_unit="impacts",
                    missile="Large Missile D", missile_weight=9.0,
                    impact_velocity=50.0)
    elif code == "GAUGE_COUNT":
        base.update(required_value=3.0)
    elif kind == "Enum":
        base.update(required_option="Full")
    elif kind == "Not Applicable":
        base.update(required_option="ASTM F588 Grade 40")
    base.update(over)
    return AtMirrorSection(**base)


class _Base(unittest.TestCase):
    def setUp(self):
        self.client, self.Session = _client_and_session()
        from app.airtable.mirror import (AtMirrorProject, AtMirrorProtocol,
                                         AtMirrorSpecimen)
        from app.data.models import Device
        s = self.Session()
        try:
            s.add(Device(id=1, name="system-1", turbo_mode=False,
                         turbo_slave=False))
            s.add(AtMirrorProject(record_id=PROJ, job_number="IFET-26-0099",
                                  project_name="Test job"))
            s.add(AtMirrorSpecimen(record_id=SPEC, specimen_name="90 Series SGD",
                                   project_record_id=PROJ))
            s.add(AtMirrorProtocol(record_id=PROT, protocol_name="ASTM E1886",
                                   specimen_record_id=SPEC))
            s.commit()
        finally:
            s.close()

    def tearDown(self):
        from app import main
        main.app.dependency_overrides.clear()

    def mirror_sections(self, *sections):
        s = self.Session()
        try:
            for sec in sections:
                s.add(sec)
            s.commit()
        finally:
            s.close()

    def full_protocol(self):
        self.mirror_sections(
            section("recSEC_STATIC", "STATIC_PRESSURE"),
            section("recSEC_CYCLIC", "CYCLIC_PRESSURE"),
            section("recSEC_IMPACT", "IMPACT_LMI"),
            section("recSEC_FE", "FORCED_ENTRY"),
            section("recSEC_ANSI", "ANSI_IMPACT",
                    required_option="Class A"),
            section("recSEC_GAUGE", "GAUGE_COUNT"))

    def do_import(self, **over):
        body = {"device_id": 1, "project_record_id": PROJ,
                "specimen_record_id": SPEC, "protocol_record_id": PROT}
        body.update(over)
        return self.client.post("/airtable/import", json=body)


class TheMirrorIsAllowlisted(_Base):
    """The boundary the change document describes as an application decision."""

    def test_a_field_outside_the_allowlist_is_refused_not_filtered(self):
        from app.airtable import mirror
        with self.assertRaises(mirror.AllowlistViolation) as caught:
            mirror.assert_allowlisted(
                mirror.PROJECT_FIELDS,
                {"IFET job number": "x", "Balance Due": 4200})
        message = str(caught.exception)
        self.assertIn("Balance Due", message)
        self.assertIn("outside the boundary", message)

    def test_value_is_named_as_never_readable(self):
        """§10.19 — the extractor's shifted column, refused by name.

        A shifted value has already reached a record marked Passed, and every
        shifted value is individually plausible. So `Value` is not merely absent
        from the allowlist; reading it is called out as a different class of
        mistake from reading billing data.
        """
        from app.airtable import mirror
        self.assertIn("Value", mirror.FORBIDDEN_FIELDS)
        self.assertNotIn("Value", mirror.SECTION_FIELDS)
        with self.assertRaises(mirror.AllowlistViolation) as caught:
            mirror.assert_allowlisted(mirror.SECTION_FIELDS, {"Value": "+60/60"})
        self.assertIn("extractor", str(caught.exception))

    def test_the_mirror_has_no_column_for_value(self):
        """Not just unread — nowhere to put it."""
        from app.airtable.mirror import AtMirrorSection
        columns = {c.name for c in AtMirrorSection.__table__.columns}
        self.assertNotIn("value", columns)
        self.assertNotIn("Value", columns)

    def test_a_refresh_is_idempotent(self):
        """Re-reading updates in place, which is what makes re-import safe."""
        from app.airtable import mirror

        class StubClient:
            def __init__(self):
                self.calls = 0

            def list_records(self, table_id, fields=None, offset=None, **kw):
                self.calls += 1
                if table_id != "tblLYcRC7q6Srjfk3":
                    return {"records": []}
                # Only allowlisted fields come back, because only they are asked
                # for — `fields` is passed through to Airtable.
                assert set(fields) == set(mirror.PROJECT_FIELDS), fields
                return {"records": [{"id": PROJ,
                                     "fields": {"IFET job number": "IFET-26-0099",
                                                "Project name": "Renamed"}}]}

        s = self.Session()
        try:
            client = StubClient()
            mirror.refresh(s, client)
            mirror.refresh(s, client)
            s.commit()
            rows = s.query(mirror.AtMirrorProject).all()
        finally:
            s.close()
        self.assertEqual(len(rows), 1, "a second refresh must not duplicate")
        self.assertEqual(rows[0].project_name, "Renamed",
                         "and must pick up the change")


class RequirementsAreValidatedNotAssumed(_Base):
    """Contract §3.2, which the change document asserts and which did not exist."""

    def test_a_unit_that_contradicts_its_kind_is_refused(self):
        from app.airtable import requirements as req
        bad = section("recSEC_BAD", "STATIC_PRESSURE", required_unit="in")
        with self.assertRaises(req.RequirementError) as caught:
            req.validate(bad)
        self.assertIn("different test", str(caught.exception))

    def test_a_blank_design_pressure_is_refused_not_zero(self):
        from app.airtable import requirements as req
        bad = section("recSEC_BAD", "STATIC_PRESSURE",
                      required_value_outward=None)
        with self.assertRaises(req.RequirementError) as caught:
            req.validate(bad)
        self.assertIn("never zero", str(caught.exception))

    def test_a_pass_fail_test_carrying_a_number_is_refused(self):
        from app.airtable import requirements as req
        bad = section("recSEC_BAD", "FORCED_ENTRY", required_value=40.0)
        with self.assertRaises(req.RequirementError):
            req.validate(bad)

    def test_blank_applicability_means_unconfirmed_never_not_required(self):
        from app.airtable import requirements as req
        self.assertEqual(
            req.applicability_of(section("s", "FORCED_ENTRY",
                                         applicability=None)),
            req.UNCONFIRMED)

    def test_an_unknown_code_routes_nowhere_and_says_so(self):
        from app.airtable import requirements as req
        from app.airtable.mirror import AtMirrorSection
        with self.assertRaises(req.RequirementError) as caught:
            req.validate(AtMirrorSection(record_id="s", protocol_record_id=PROT,
                                         requirement_code="SOMETHING_NEW"))
        self.assertIn("not one LabOS knows", str(caught.exception))

    def test_the_sections_route_reports_refusals_rather_than_omitting_them(self):
        self.mirror_sections(
            section("recSEC_OK", "FORCED_ENTRY"),
            section("recSEC_BAD", "STATIC_PRESSURE", required_unit="in"))
        body = self.client.get(f"/airtable/protocols/{PROT}/sections").json()
        by_id = {s["record_id"]: s for s in body["sections"]}
        self.assertTrue(by_id["recSEC_OK"]["executable"])
        self.assertIsNone(by_id["recSEC_OK"]["refused"])
        self.assertFalse(by_id["recSEC_BAD"]["executable"])
        self.assertIn("different test", by_id["recSEC_BAD"]["refused"],
                      "a section LabOS will not run is the one thing the "
                      "operator can fix, so the reason must reach them")


class ImportGoesThroughTheOneCreatePath(_Base):

    def test_it_prefills_and_derives_the_fourteen_stages(self):
        self.full_protocol()
        r = self.do_import()
        self.assertEqual(r.status_code, 200, r.text)
        project = r.json()
        self.assertEqual(project["inward_design_pressure"], 60.0)
        self.assertEqual(project["outward_design_pressure"], 45.0)
        self.assertEqual(project["name"], "90 Series SGD")
        # The same six and eight a typed project gets — because it is the same
        # function that made them.
        self.assertEqual(len(project["static_tests"]), 6)
        self.assertEqual(len(project["cyclic_tests"]), 8)

    def test_a_typed_project_and_an_imported_one_derive_identical_stages(self):
        """The property that makes one create path worth insisting on."""
        self.full_protocol()
        imported = self.do_import().json()
        typed = self.client.post("/devices/1/projects/", json={
            "name": "typed by hand", "inward_design_pressure": 60.0,
            "outward_design_pressure": 45.0}).json()
        self.assertEqual([t["pressure"] for t in imported["static_tests"]],
                         [t["pressure"] for t in typed["static_tests"]])
        self.assertEqual([t["high_pressure"] for t in imported["cyclic_tests"]],
                         [t["high_pressure"] for t in typed["cyclic_tests"]])

    def test_each_test_type_is_bound_to_its_own_section(self):
        self.full_protocol()
        project = self.do_import().json()
        from app.data.models import (CyclicTest, ManualTest, MissileImpactTest,
                                     StaticTest)
        s = self.Session()
        try:
            self.assertEqual(
                {t.airtable_section_id for t in s.query(StaticTest).all()},
                {"recSEC_STATIC"})
            self.assertEqual(
                {t.airtable_section_id for t in s.query(CyclicTest).all()},
                {"recSEC_CYCLIC"})
            self.assertEqual(
                {t.airtable_section_id for t in s.query(MissileImpactTest).all()},
                {"recSEC_IMPACT"})
            self.assertEqual(
                {(t.type, t.airtable_section_id)
                 for t in s.query(ManualTest).all()},
                {("Forced Entry", "recSEC_FE"), ("ANSI Z97.1", "recSEC_ANSI")})
        finally:
            s.close()
        self.assertEqual(project["gauge_count"], 3,
                         "GAUGE_COUNT is a parameter, not a test")
        self.assertEqual(project["impact_count"], 3)

    def test_two_impact_sections_sum_rather_than_overwrite(self):
        """LMI and SMI are different missiles, each with its own count.

        `out.impact_count` was assigned rather than accumulated, so a protocol
        requiring 2 large-missile impacts and 3 small-missile ones kept whichever
        section the mirror returned last. Both values are individually plausible,
        which is why nothing caught it — and the product owner's confirmation
        that one impact means one attempt makes this figure load-bearing: it is
        what says how many attempt rows a protocol should produce.
        """
        self.mirror_sections(
            section("recSEC_STATIC", "STATIC_PRESSURE"),
            section("recSEC_LMI", "IMPACT_LMI", required_value=2.0),
            section("recSEC_SMI", "IMPACT_SMI", required_value=3.0))
        r = self.do_import()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["impact_count"], 5)

    def test_two_impact_sections_are_two_tests(self):
        """The reason summing is right and refusing would not be.

        Two design-pressure sections that disagree cannot both hold — all
        fourteen stages come from one pair. Two impact sections that differ are
        not a contradiction at all: each becomes its own test.
        """
        from app.data.models import MissileImpactTest
        self.mirror_sections(
            section("recSEC_STATIC", "STATIC_PRESSURE"),
            section("recSEC_LMI", "IMPACT_LMI", required_value=2.0),
            section("recSEC_SMI", "IMPACT_SMI", required_value=3.0,
                    missile="Small Missile A", missile_weight=2.0))
        self.assertEqual(self.do_import().status_code, 200)
        s = self.Session()
        try:
            tests = s.query(MissileImpactTest).all()
            self.assertEqual(len(tests), 2)
            self.assertEqual({t.airtable_section_id for t in tests},
                             {"recSEC_LMI", "recSEC_SMI"})
            # Each carries its own section's family. `missile` is **no longer
            # pre-filled** — `Missile Type` left the read contract on
            # 2026-09-10 — so both are None even though both sections carry a
            # value in the mirror. That absence is the assertion.
            self.assertEqual({t.impact_family for t in tests}, {"LMI", "SMI"})
            self.assertEqual({t.missile for t in tests}, {None})
        finally:
            s.close()

    def test_one_impact_section_is_unchanged(self):
        """Accumulating from `None` must not turn one section into a sum."""
        self.full_protocol()
        self.assertEqual(self.do_import().json()["impact_count"], 3)

    def test_a_repeated_import_returns_the_same_project(self):
        self.full_protocol()
        first = self.do_import().json()
        second = self.do_import()
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["id"], first["id"])
        from app.data.models import Project, StaticTest
        s = self.Session()
        try:
            self.assertEqual(s.query(Project).count(), 1)
            self.assertEqual(s.query(StaticTest).count(), 6,
                             "a re-import must not produce a second set of "
                             "tests against the same specimen")
        finally:
            s.close()

    def test_an_unconfirmed_section_is_not_executed_and_not_dropped(self):
        self.mirror_sections(
            section("recSEC_STATIC", "STATIC_PRESSURE"),
            section("recSEC_FE", "FORCED_ENTRY", applicability=None))
        plan = self.client.post("/airtable/import/plan", json={
            "device_id": 1, "project_record_id": PROJ,
            "specimen_record_id": SPEC, "protocol_record_id": PROT}).json()
        self.assertEqual(plan["unconfirmed"], ["recSEC_FE"])
        self.assertEqual([e["code"] for e in plan["executable"]],
                         ["STATIC_PRESSURE"])

    def test_import_without_a_design_pressure_pair_is_refused(self):
        self.mirror_sections(section("recSEC_FE", "FORCED_ENTRY"))
        r = self.do_import()
        self.assertEqual(r.status_code, 400)
        self.assertIn("invented requirement", r.json()["detail"])

    def test_a_mismatched_hierarchy_is_refused(self):
        from app.airtable.mirror import AtMirrorSpecimen
        self.full_protocol()
        s = self.Session()
        try:
            s.add(AtMirrorSpecimen(record_id="recOTHER00000001",
                                   specimen_name="someone else's",
                                   project_record_id="recDIFFERENT00001"))
            s.commit()
        finally:
            s.close()
        r = self.do_import(specimen_record_id="recOTHER00000001")
        self.assertEqual(r.status_code, 400)
        self.assertIn("wrong mock-up", r.json()["detail"])

    def test_two_disagreeing_design_pairs_are_refused_not_averaged(self):
        self.mirror_sections(
            section("recSEC_STATIC", "STATIC_PRESSURE"),
            section("recSEC_CYCLIC", "CYCLIC_PRESSURE",
                    required_value_inward=70.0, required_value_outward=55.0))
        plan = self.client.post("/airtable/import/plan", json={
            "device_id": 1, "project_record_id": PROJ,
            "specimen_record_id": SPEC, "protocol_record_id": PROT}).json()
        reasons = [r["reason"] for r in plan["refused"]]
        self.assertTrue(any("cannot both hold" in r for r in reasons), reasons)

    def test_reads_never_touch_airtable(self):
        """An empty mirror is an empty picker, not a blocked operator."""
        s = self.Session()
        try:
            s.query.__self__  # noqa: B018 - session is live
        finally:
            s.close()
        for path in (f"/airtable/projects",
                     f"/airtable/projects/{PROJ}/specimens",
                     f"/airtable/specimens/{SPEC}/protocols",
                     f"/airtable/protocols/{PROT}/sections"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, f"{path}: {r.text}")


class TheRequirementIsFrozenAtStart(_Base):

    def test_an_upstream_edit_cannot_change_what_a_finished_test_claims(self):
        """The reason freezing is not optional.

        A requirement edited after a test has run must not change what that test
        reports having been run against. Stale would be forgivable; false is not.
        """
        self.full_protocol()
        project = self.do_import().json()
        from app.data.models import ManualTest
        s = self.Session()
        try:
            manual = (s.query(ManualTest)
                      .filter(ManualTest.type == "Forced Entry").one())
            test_id = manual.id
        finally:
            s.close()

        started = self.client.post(
            f"/projects/{project['id']}/manual-tests/{test_id}/trials",
            json={"operator_name": "technician-1"})
        self.assertEqual(started.status_code, 200, started.text)
        aid = started.json()["labos_attempt_id"]

        # Somebody edits the requirement upstream, and the mirror is refreshed.
        s = self.Session()
        try:
            from app.airtable.mirror import AtMirrorSection
            sec = s.get(AtMirrorSection, "recSEC_FE")
            sec.required_option = "ASTM F588 Grade 10"
            s.commit()

            from app.data.models import TestResult
            row = s.query(TestResult).filter(
                TestResult.labos_attempt_id == aid).one()
            snap = row.requirement_snapshot
        finally:
            s.close()

        self.assertIsNotNone(snap, "the requirement must be frozen at start")
        self.assertEqual(snap["required_option"], "ASTM F588 Grade 40",
                         "the frozen requirement is what the test was run "
                         "against, not what the section says now")
        self.assertEqual(snap["airtable_section_id"], "recSEC_FE")

    def test_a_local_job_has_nothing_to_freeze_and_that_is_normal(self):
        created = self.client.post("/devices/1/projects/", json={
            "name": "local only", "inward_design_pressure": 60.0,
            "outward_design_pressure": 45.0}).json()
        made = self.client.post(f"/projects/{created['id']}/manual-tests/",
                                json={"type": "Forced Entry",
                                      "required_option": "typed by hand"})
        started = self.client.post(
            f"/projects/{created['id']}/manual-tests/{made.json()['id']}/trials",
            json={"operator_name": "technician-1"})
        self.assertEqual(started.status_code, 200, started.text)
        from app.data.models import TestResult
        s = self.Session()
        try:
            row = s.query(TestResult).filter(
                TestResult.labos_attempt_id
                == started.json()["labos_attempt_id"]).one()
            self.assertIsNone(row.requirement_snapshot)
        finally:
            s.close()


class TheImpactFamilyIsFrozenFromTheRequirementCode(_Base):
    """Airtable owns SMI vs LMI, and the operator is never asked.

    `bind` sets it once from IMPACT_SMI / IMPACT_LMI. The import route returns
    early on an existing project, so `bind` does not re-run on a refresh —
    which is what makes the value frozen structurally rather than by a rule
    something has to remember to enforce.
    """

    def _import(self):
        r = self.do_import()
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def test_an_lmi_section_freezes_the_family_as_lmi(self):
        self.mirror_sections(section("recSEC_STATIC", "STATIC_PRESSURE"),
                             section("recSEC_IMPACT", "IMPACT_LMI"))
        tests = self._import()["missile_impact_tests"]
        self.assertEqual(["LMI"], [t["impact_family"] for t in tests])

    def test_an_smi_section_freezes_the_family_as_smi(self):
        self.mirror_sections(section("recSEC_STATIC", "STATIC_PRESSURE"),
                             section("recSEC_IMPACT", "IMPACT_SMI"))
        tests = self._import()["missile_impact_tests"]
        self.assertEqual(["SMI"], [t["impact_family"] for t in tests])

    def test_two_impact_sections_get_their_own_families(self):
        """LMI and SMI are two tests, each carrying its own section's code."""
        self.mirror_sections(section("recSEC_STATIC", "STATIC_PRESSURE"),
                             section("recSEC_LMI", "IMPACT_LMI"),
                             section("recSEC_SMI", "IMPACT_SMI"))
        tests = self._import()["missile_impact_tests"]
        self.assertEqual({"LMI", "SMI"}, {t["impact_family"] for t in tests})

    def test_the_level_and_velocity_are_not_prefilled_from_airtable(self):
        """Only the family comes from Airtable. The level is the operator's
        choice and the target velocity is operator-entered — neither is
        derived, and nothing in Airtable supplies them."""
        self.mirror_sections(section("recSEC_STATIC", "STATIC_PRESSURE"),
                             section("recSEC_IMPACT", "IMPACT_LMI"))
        test = self._import()["missile_impact_tests"][0]
        self.assertIsNone(test["impact_level"])
        self.assertIsNone(test["target_velocity"])
        self.assertIsNone(test["impact_classification"])

    def test_a_bound_test_carries_the_section_id_that_owns_its_family(self):
        """The authority is resource-level: this column on this row."""
        self.mirror_sections(section("recSEC_STATIC", "STATIC_PRESSURE"),
                             section("recSEC_IMPACT", "IMPACT_LMI"))
        test = self._import()["missile_impact_tests"][0]
        self.assertEqual("recSEC_IMPACT", test["airtable_section_id"])


class AnUnsupportedStaticProgrammeSuppressesStaticLoad(_Base):
    """`STATIC_PROGRAMME` was read, validated and then discarded.

    Contract §3.1 has always said the "initial static programme supports
    `Full`", and until 2026-09-10 nothing enforced it: `importer.plan` hit
    `continue` on the code and LabOS ran its own six-stage sequence whatever
    the proposal asked for.

    Reporting the mismatch is **not** enough, which is what these tests pin.
    `ImportPlan.refused` is serialised by `as_dict` and consumed by nothing, so
    a refusal that stopped there would have printed a line in an import report
    and then run the Full programme anyway. The requirement is that Static Load
    stops being executable — and that nothing else does.
    """

    def _plan(self):
        return self.client.post("/airtable/import/plan", json={
            "device_id": 1, "project_record_id": PROJ,
            "specimen_record_id": SPEC, "protocol_record_id": PROT}).json()

    def test_full_is_supported_and_changes_nothing(self):
        self.full_protocol()
        self.mirror_sections(section("recSEC_PROG", "STATIC_PROGRAMME",
                                     required_option="Full"))
        plan = self._plan()
        codes = [e["code"] for e in plan["executable"]]
        self.assertIn("STATIC_PRESSURE", codes)
        self.assertIsNone(plan["static_unavailable"])
        self.assertEqual([], [r for r in plan["refused"]], plan["refused"])

    def test_an_unsupported_programme_makes_static_load_non_executable(self):
        self.full_protocol()
        self.mirror_sections(section("recSEC_PROG", "STATIC_PROGRAMME",
                                     required_option="Partial"))
        plan = self._plan()
        codes = [e["code"] for e in plan["executable"]]
        self.assertNotIn("STATIC_PRESSURE", codes)
        self.assertIsNotNone(plan["static_unavailable"])
        self.assertIn("Partial", plan["static_unavailable"])

    def test_it_suppresses_static_load_and_nothing_else(self):
        self.full_protocol()
        self.mirror_sections(section("recSEC_PROG", "STATIC_PROGRAMME",
                                     required_option="Partial"))
        codes = {e["code"] for e in self._plan()["executable"]}
        self.assertEqual({"CYCLIC_PRESSURE", "IMPACT_LMI", "FORCED_ENTRY",
                          "ANSI_IMPACT"}, codes)

    def test_the_design_pressures_survive_because_cycles_needs_them(self):
        """Cycles derives its eight stages from the same pair and has no
        programme of its own, so suppressing the pair would break a test the
        proposal did not ask a question about."""
        self.full_protocol()
        self.mirror_sections(section("recSEC_PROG", "STATIC_PROGRAMME",
                                     required_option="Partial"))
        self.assertEqual([60.0, 45.0], self._plan()["design_pressures"])

    def test_the_reason_names_the_programme_and_says_what_is_unavailable(self):
        self.full_protocol()
        self.mirror_sections(section("recSEC_PROG", "STATIC_PROGRAMME",
                                     required_option="Reduced"))
        reasons = " ".join(r["reason"] for r in self._plan()["refused"])
        self.assertIn("Reduced", reasons)
        self.assertIn("not supported", reasons)
        self.assertIn("Static Load is unavailable", reasons)

    def test_the_static_section_itself_is_refused_not_silently_dropped(self):
        """An operator must be able to see *which* section stopped working."""
        self.full_protocol()
        self.mirror_sections(section("recSEC_PROG", "STATIC_PROGRAMME",
                                     required_option="Partial"))
        refused = {r["section"] for r in self._plan()["refused"]}
        self.assertIn("recSEC_STATIC", refused)
        self.assertIn("recSEC_PROG", refused)

    def test_it_works_when_the_programme_section_is_read_first(self):
        """Sections are ordered by record id, so the programme can arrive
        before or after the pressure section it suppresses. The gate runs after
        the loop for exactly this reason.

        Note this is the case the rest of this class happens to exercise
        anyway: `recSEC_PROG` sorts before `recSEC_STATIC`. The one that
        matters is the sibling test below.
        """
        self.mirror_sections(
            section("recSEC_AAA_PROG", "STATIC_PROGRAMME",
                    required_option="Partial"),
            section("recSEC_ZZZ_STATIC", "STATIC_PRESSURE"))
        codes = [e["code"] for e in self._plan()["executable"]]
        self.assertNotIn("STATIC_PRESSURE", codes)

    def test_it_works_when_the_programme_section_is_read_last(self):
        """**The case an inline check would have failed.**

        Here the pressure section is already in `out.executable` by the time
        the programme is seen, so suppression cannot be a branch inside the
        loop — it has to run after it. Reversing the ids is the whole test.
        """
        self.mirror_sections(
            section("recSEC_AAA_STATIC", "STATIC_PRESSURE"),
            section("recSEC_ZZZ_PROG", "STATIC_PROGRAMME",
                    required_option="Partial"))
        plan = self._plan()
        self.assertNotIn("STATIC_PRESSURE",
                         [e["code"] for e in plan["executable"]])
        self.assertIn("recSEC_AAA_STATIC",
                      {r["section"] for r in plan["refused"]})

    def test_both_orderings_produce_the_same_plan(self):
        """Order-independence stated as one assertion rather than inferred
        from two tests that happen to agree."""
        def plan_for(prog_id, static_id):
            s = self.Session()
            try:
                from app.airtable.mirror import AtMirrorSection
                s.query(AtMirrorSection).delete()
                s.commit()
            finally:
                s.close()
            self.mirror_sections(
                section(static_id, "STATIC_PRESSURE"),
                section(prog_id, "STATIC_PROGRAMME", required_option="Partial"))
            p = self._plan()
            return ([e["code"] for e in p["executable"]],
                    p["static_unavailable"])

        self.assertEqual(plan_for("recSEC_A_PROG", "recSEC_Z_STATIC"),
                         plan_for("recSEC_Z_PROG", "recSEC_A_STATIC"))

    def test_a_blank_programme_is_already_refused_by_kind_validation(self):
        """Not this gate's job: an Enum requirement with no option never
        reaches it."""
        self.full_protocol()
        self.mirror_sections(section("recSEC_PROG", "STATIC_PROGRAMME",
                                     required_option=None))
        reasons = " ".join(r["reason"] for r in self._plan()["refused"])
        self.assertIn("Required Option is blank", reasons)


if __name__ == "__main__":
    unittest.main()
