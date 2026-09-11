"""DG14 — an unverified Airtable requirement cannot release a rig.

Contract §3.3, and §3.3 says where: *"Check this on the backend start path,
not only in a UI."*

**The thing being defended against is undetectable by inspection.** Their PDF
extractor reads values in order rather than by column, so a blank cell slides
everything after it one place left and a 60 PSF requirement arrives as 9 — a
number, in the right column, with the right unit, of the right kind. No
assertion in this file looks at a value and judges it, and none should: the
control is that **two independent readings of the same fact must agree**, and
that a disagreement stops the rig instead of being resolved.

The chain every one of these tests walks:

    raw Airtable row -> mirror -> requirements.validate -> typed inward/outward
    PSF -> independently verified by a named person -> frozen snapshot
    -> executable

Anything that breaks the chain leaves the job **visible and non-executable with
a stated reason**, which is the other half of the requirement: an operator who
is merely refused, with no explanation, re-imports at random.
"""

import unittest

from tests.test_inbound_import import _Base, section

VERIFY = {"inward_psf": 60.0, "outward_psf": 45.0, "unit": "PSF",
          "reference": "Proposal P-2291 rev C", "verified_by": "technician-1"}

TRIAL = {"operator_name": "technician-1", "result": True,
         "testing_continued": "Stopped",
         "deflections": [{"deflection_gauge": "g1", "max_deflection": 1234.0,
                          "permanent_deflection": 12.0, "recovery": 60.0}]}


class _Gate(_Base):
    """An imported job with a static and a cyclic section, nothing verified."""

    def imported(self, static_over=None, cyclic_over=None):
        self.mirror_sections(
            section("recSEC_STATIC", "STATIC_PRESSURE", **(static_over or {})),
            section("recSEC_CYCLIC", "CYCLIC_PRESSURE", **(cyclic_over or {})),
            section("recSEC_GAUGE", "GAUGE_COUNT"))
        r = self.do_import()
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def local_project(self):
        """A LabOS-only job: the operator typed the pressures here."""
        r = self.client.post("/devices/1/projects/", json={
            "name": "local job", "inward_design_pressure": 60.0,
            "outward_design_pressure": 45.0, "has_water_infiltration": False})
        self.assertIn(r.status_code, (200, 201), r.text)
        return r.json()["id"]

    def assert_blocked(self, pid, code=None):
        """Every rig entry point refuses, and says why."""
        for method, path, body in (
                ("put", f"/projects/{pid}/static_tests/0/start",
                 {"operator_name": "technician-1"}),
                ("post", f"/projects/{pid}/static_tests/0/trials", TRIAL),
                ("put", f"/projects/{pid}/cyclic_tests/0/start",
                 {"operator_name": "technician-1"}),
                ("post", f"/projects/{pid}/cyclic-tests/0/trials", TRIAL)):
            r = getattr(self.client, method)(path, json=body)
            self.assertEqual(
                r.status_code, 409,
                f"{method.upper()} {path} let an unreleased requirement run: "
                f"{r.status_code} {r.text[:200]}")
            self.assertTrue(r.json()["detail"].strip(),
                            "a refusal with no reason is how an operator ends "
                            "up re-importing at random")
        if code:
            state = self.client.get(
                f"/projects/{pid}/requirement-release").json()
            self.assertFalse(state["executable"])
            self.assertEqual(state["code"], code, state["reason"])

    def assert_runs(self, pid):
        self.assertEqual(
            self.client.put(f"/projects/{pid}/static_tests/0/start",
                            json={"operator_name": "technician-1"}).status_code,
            200)
        r = self.client.post(f"/projects/{pid}/static_tests/0/trials", json=TRIAL)
        self.assertIn(r.status_code, (200, 201), r.text)
        return r


class NothingUnverifiedReachesARig(_Gate):

    def test_an_imported_job_cannot_run_until_it_is_verified(self):
        pid = self.imported()
        self.assert_blocked(pid, code="unverified")

    def test_verifying_it_releases_exactly_that_job(self):
        pid = self.imported()
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json=VERIFY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["executable"])
        self.assert_runs(pid)

    def test_a_labos_only_job_is_unaffected(self):
        """A9's promise, and it is still true of exactly this case.

        The operator typed these pressures here. There is no second source to
        reconcile and no extractor in the path, so there is nothing to verify
        and nothing to gate.
        """
        pid = self.local_project()
        state = self.client.get(f"/projects/{pid}/requirement-release").json()
        self.assertTrue(state["executable"])
        self.assertEqual(state["code"], "local")
        self.assert_runs(pid)

    def test_a_local_job_has_nothing_to_verify_and_says_so(self):
        pid = self.local_project()
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json=VERIFY)
        self.assertEqual(r.status_code, 400, r.text)


class TheShiftedValueIsCaughtByDisagreementNotByInspection(_Gate):
    """The production defect, in the shape it actually takes.

    `live-probe-findings-2026-08-23.md` §5.2: a blank cell slid every value
    after it one column left, and a +60/60 design pressure arrived as the 9
    that belonged to `# Dials`. **9 PSF is a perfectly well-formed
    requirement** — it validates, it is the right kind, it carries the right
    unit — so nothing that examines it can tell.
    """

    SHIFTED = {"required_value_inward": 9.0, "required_value_outward": 9.0}

    def test_the_shifted_pair_is_well_formed_and_that_is_the_point(self):
        from app.airtable import requirements as req
        req.validate(section("recSEC_STATIC", "STATIC_PRESSURE", **self.SHIFTED))

    def test_a_shifted_requirement_cannot_reach_the_rig(self):
        pid = self.imported(static_over=self.SHIFTED, cyclic_over=self.SHIFTED)
        # Unverified: blocked, like any imported job.
        self.assert_blocked(pid, code="unverified")
        # And the verification is what catches it: the operator read 60/45 off
        # the proposal, Airtable holds 9/9, and the two do not agree.
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json=VERIFY)
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()["detail"]
        self.assertIn("9.0", detail)
        self.assertIn("60.0", detail)
        self.assert_blocked(pid, code="unverified")

    def test_the_refused_verification_is_not_stored(self):
        """Nothing is written on a disagreement.

        Storing it would leave the job permanently non-executable with a
        number in it that nobody believes, and no way back.
        """
        pid = self.imported(static_over=self.SHIFTED, cyclic_over=self.SHIFTED)
        self.client.post(f"/projects/{pid}/requirement-verification", json=VERIFY)
        from app.data.models import Project
        s = self.Session()
        try:
            p = s.get(Project, pid)
            self.assertIsNone(p.requirement_verified_inward)
            self.assertIsNone(p.requirement_verified_by)
            self.assertIsNone(p.requirement_verified_at)
        finally:
            s.close()

    def test_labos_does_not_pick_a_winner(self):
        """The imported pair is not overwritten by the verified one.

        Running on the operator's numbers while Airtable's record says
        something else would publish a result claiming a requirement it was
        not run against. The disagreement is escalated, not resolved.
        """
        pid = self.imported(static_over=self.SHIFTED, cyclic_over=self.SHIFTED)
        self.client.post(f"/projects/{pid}/requirement-verification", json=VERIFY)
        from app.data.models import Project
        s = self.Session()
        try:
            self.assertEqual(s.get(Project, pid).inward_design_pressure, 9.0)
        finally:
            s.close()


class TheChainRefusesEveryBrokenShape(_Gate):
    """Malformed, ambiguous, wrong unit, half a pair, unsupported.

    Most of these are refused at import — the job never exists — which is a
    stronger outcome than a gate. The ones that can only appear *after* import,
    because the mirror is refreshed, are caught by the gate re-validating.
    """

    def _import_refused(self, **over):
        self.mirror_sections(
            section("recSEC_STATIC", "STATIC_PRESSURE", **over),
            section("recSEC_GAUGE", "GAUGE_COUNT"))
        r = self.do_import()
        self.assertEqual(r.status_code, 400, f"expected a refusal: {r.text[:200]}")
        return r.json()["detail"]

    def test_a_missing_direction_cannot_execute(self):
        self._import_refused(required_value_outward=None)

    def test_a_unit_mismatch_cannot_execute(self):
        self._import_refused(required_unit="in")

    def test_a_kind_that_contradicts_its_code_cannot_execute(self):
        self._import_refused(requirement_kind="Count")

    def test_an_unknown_code_cannot_execute(self):
        self.mirror_sections(section("recSEC_GAUGE", "GAUGE_COUNT"))
        from app.airtable.mirror import AtMirrorSection
        s = self.Session()
        try:
            s.add(AtMirrorSection(
                record_id="recSEC_WAT", protocol_record_id="recPROT000000001",
                section_name="something new", requirement_code="ROCKET_SLED",
                applicability="Required"))
            s.commit()
        finally:
            s.close()
        r = self.do_import()
        self.assertEqual(r.status_code, 400, r.text)

    def _drift(self, pid, **over):
        """Edit the mirror after import — an upstream change, as happens."""
        from app.airtable.mirror import AtMirrorSection
        s = self.Session()
        try:
            for rec in ("recSEC_STATIC", "recSEC_CYCLIC"):
                sec = s.get(AtMirrorSection, rec)
                for k, v in over.items():
                    setattr(sec, k, v)
            s.commit()
        finally:
            s.close()

    def test_a_requirement_edited_after_import_stops_the_rig(self):
        """The gate re-validates rather than trusting import-time truth."""
        pid = self.imported()
        self.client.post(f"/projects/{pid}/requirement-verification", json=VERIFY)
        self.assert_runs(pid)
        self._drift(pid, required_value_inward=110.0)
        self.assert_blocked(pid, code="project_drifted_from_mirror")

    def test_a_requirement_made_malformed_after_import_stops_the_rig(self):
        pid = self.imported()
        self.client.post(f"/projects/{pid}/requirement-verification", json=VERIFY)
        self._drift(pid, required_unit="in")
        self.assert_blocked(pid, code="invalid_requirement")

    def test_a_section_deleted_upstream_stops_the_rig(self):
        pid = self.imported()
        self.client.post(f"/projects/{pid}/requirement-verification", json=VERIFY)
        from app.airtable.mirror import AtMirrorSection
        s = self.Session()
        try:
            for rec in ("recSEC_STATIC", "recSEC_CYCLIC"):
                s.delete(s.get(AtMirrorSection, rec))
            s.commit()
        finally:
            s.close()
        self.assert_blocked(pid, code="section_missing")


class AsymmetricPairsStillWork(_Gate):
    """Inward and outward are independent and must stay so.

    A transposition would be individually plausible, which is the same
    property that makes the shift undetectable — so the pair is carried and
    compared as an ordered pair, never collapsed to one scalar.
    """

    def test_an_asymmetric_pair_verifies_and_runs(self):
        pid = self.imported(
            static_over={"required_value_inward": 75.0,
                         "required_value_outward": 52.5},
            cyclic_over={"required_value_inward": 75.0,
                         "required_value_outward": 52.5})
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json={**VERIFY, "inward_psf": 75.0,
                                   "outward_psf": 52.5})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["verified_pair_psf"], [75.0, 52.5])
        self.assert_runs(pid)

    def test_a_transposed_pair_is_refused(self):
        pid = self.imported(
            static_over={"required_value_inward": 75.0,
                         "required_value_outward": 52.5},
            cyclic_over={"required_value_inward": 75.0,
                         "required_value_outward": 52.5})
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json={**VERIFY, "inward_psf": 52.5,
                                   "outward_psf": 75.0})
        self.assertEqual(r.status_code, 409, r.text)

    def test_the_derived_stages_still_come_from_the_pair(self):
        pid = self.imported(
            static_over={"required_value_inward": 75.0,
                         "required_value_outward": 52.5},
            cyclic_over={"required_value_inward": 75.0,
                         "required_value_outward": 52.5})
        self.client.post(f"/projects/{pid}/requirement-verification",
                         json={**VERIFY, "inward_psf": 75.0,
                               "outward_psf": 52.5})
        proj = self.client.get("/devices/1/projects/").json()
        proj = [p for p in proj if p["id"] == pid][0]
        self.assertEqual(len(proj["static_tests"]), 6)
        self.assertEqual(len(proj["cyclic_tests"]), 8)
        self.assertEqual(proj["inward_design_pressure"], 75.0)
        self.assertEqual(proj["outward_design_pressure"], 52.5)


class TheVerificationIsAnAssertionSomebodySigned(_Gate):
    """§3.3: an `operator` provenance tag alone is insufficient."""

    def test_a_blank_reference_is_refused(self):
        pid = self.imported()
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json={**VERIFY, "reference": "   "})
        self.assertEqual(r.status_code, 409, r.text)

    def test_a_blank_verifier_is_refused(self):
        pid = self.imported()
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json={**VERIFY, "verified_by": ""})
        self.assertEqual(r.status_code, 409, r.text)

    def test_a_pair_in_the_wrong_unit_is_refused_not_converted(self):
        pid = self.imported()
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json={**VERIFY, "unit": "psi"})
        self.assertEqual(r.status_code, 409, r.text)

    def test_the_verification_is_frozen_onto_the_attempt(self):
        """§3.3: verification facts are frozen at run creation.

        Looked up later, an attempt would report a verification that may have
        happened after it ran.
        """
        pid = self.imported()
        self.client.post(f"/projects/{pid}/requirement-verification", json=VERIFY)
        self.assert_runs(pid)
        from app.data.models import StaticTestResult
        s = self.Session()
        try:
            snap = s.query(StaticTestResult).first().requirement_snapshot
        finally:
            s.close()
        facts = snap["source_verification"]
        self.assertEqual(facts["verified_inward_psf"], 60.0)
        self.assertEqual(facts["verified_outward_psf"], 45.0)
        self.assertEqual(facts["verified_by"], "technician-1")
        self.assertEqual(facts["reference"], "Proposal P-2291 rev C")
        self.assertTrue(facts["verified_at"])

    def test_it_cannot_be_changed_once_a_rig_attempt_exists(self):
        pid = self.imported()
        self.client.post(f"/projects/{pid}/requirement-verification", json=VERIFY)
        self.assert_runs(pid)
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json=VERIFY)
        self.assertEqual(r.status_code, 409, r.text)

    def test_a_typo_can_be_re_recorded_before_any_attempt(self):
        """Caught immediately, it should not need a new job."""
        pid = self.imported()
        self.client.post(f"/projects/{pid}/requirement-verification",
                         json={**VERIFY, "verified_by": "typo"})
        r = self.client.post(f"/projects/{pid}/requirement-verification",
                             json=VERIFY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("technician-1", r.json()["reason"])


if __name__ == "__main__":
    unittest.main()
