"""Manual test capture — Impact, Forced Entry, ANSI Z97.1, through the real routes.

Delivery plan §4.5. Two levels, matching the rest of the codebase:

    manual_tests         ──trials──> manual_test_results (TestResult)
    missile_impact_tests ──trials──> impact_test_results (TestResult) ──> shots

so the flow is **create the test, then start an attempt on it**, exactly as
static and cyclic record theirs. Everything that acts on an *attempt* —
terminate, review, attach evidence — lives on `/test-results/{id}`, which is
already the polymorphic route for all five types.

These tests exercise HTTP rather than the ORM because that is where the phase
rules live: a verdict cannot precede termination, a second verdict is refused,
evidence cannot be added after review. None of that is visible from the models.

Runs against harness Postgres when `M2_DATABASE_URL` is set, SQLite otherwise.
These are application rules, not locking guarantees, so both are meaningful.
"""

import io
import os
import unittest

try:
    from fastapi.testclient import TestClient
except ImportError:                                          # pragma: no cover
    TestClient = None

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

JPEG = {"file": ("evidence.jpg", io.BytesIO(b"jpegbytes"), "image/jpeg")}


def _jpeg(name="evidence.jpg"):
    return {"file": (name, io.BytesIO(b"jpegbytes"), "image/jpeg")}


def _client_and_session():
    from app import main
    from app.data.models import Base

    url = os.environ.get("M2_DATABASE_URL", "sqlite://")
    kw = {}
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        kw = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
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


class _Base(unittest.TestCase):
    def setUp(self):
        self.client, self.Session = _client_and_session()
        from app.data.models import Device, Project, ProjectParent
        s = self.Session()
        # turbo_charger is a self-referential FK, so it stays NULL.
        s.add(Device(id=1, name="system-1", turbo_mode=False, turbo_slave=False))
        s.add(ProjectParent(id=1, name="IFET-26-0066"))
        s.add(Project(id=1, name="Specimen A", parent_id=1, device_id=1,
                      inward_design_pressure=60.0, outward_design_pressure=45.0))
        s.commit()
        s.close()

    def tearDown(self):
        from app import main
        main.app.dependency_overrides.clear()

    # -- helpers -----------------------------------------------------------

    def start(self, test_url, test_id):
        r = self.client.post(f"{test_url}/{test_id}/trials",
                             json={"operator_name": "technician-1"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def finish(self, attempt_id, **body):
        return self.client.put(f"/test-results/{attempt_id}/finish", json=body)

    def verdict(self, attempt_id, **over):
        body = {"test_result": "Pass", "verdict_by": "reviewer-1",
                "retest_required": False}
        body.update(over)
        return self.client.put(f"/test-results/{attempt_id}/verdict", json=body)


@unittest.skipIf(TestClient is None, "fastapi not installed in this environment")
class TwoLevels(_Base):
    """The test row and the attempt row are different things."""

    def _test(self, type_="Forced Entry"):
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": type_,
                                   "required_option": "ASTM F588 Grade 40"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_creating_a_test_does_not_create_an_attempt(self):
        """Create the test; the operator starts an attempt when they are ready."""
        t = self._test()
        self.assertEqual(t["trials"], [])
        self.assertFalse(t["finished"])

    def test_attempts_carry_both_identifiers(self):
        """The whole reason for two levels: the envelope needs both."""
        t = self._test()
        a = self.start("/projects/1/manual-tests", t["id"])
        self.assertTrue(a["labos_attempt_id"])
        self.assertTrue(a["labos_test_id"])
        self.assertNotEqual(a["labos_attempt_id"], a["labos_test_id"])

    def test_a_second_attempt_shares_the_test_id_and_increments_the_number(self):
        """'Attempt 2 of the same test' - inexpressible with one flat row."""
        t = self._test()
        a1 = self.start("/projects/1/manual-tests", t["id"])
        a2 = self.start("/projects/1/manual-tests", t["id"])
        self.assertEqual((a1["trial_number"], a2["trial_number"]), (1, 2))
        self.assertEqual(a1["labos_test_id"], a2["labos_test_id"])
        self.assertNotEqual(a1["labos_attempt_id"], a2["labos_attempt_id"])

    def test_attempts_are_listed_under_their_test(self):
        t = self._test()
        self.start("/projects/1/manual-tests", t["id"])
        self.start("/projects/1/manual-tests", t["id"])
        listed = self.client.get(f"/projects/1/manual-tests/{t['id']}/trials").json()
        self.assertEqual([a["trial_number"] for a in listed], [1, 2])

    def test_a_finished_test_takes_no_further_attempts(self):
        t = self._test()
        self.client.put(f"/projects/1/manual-tests/{t['id']}/finish")
        r = self.client.post(f"/projects/1/manual-tests/{t['id']}/trials", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("finished", r.text)

    def test_both_types_and_nothing_else(self):
        for ty in ("Forced Entry", "ANSI Z97.1"):
            with self.subTest(ty):
                self.assertEqual(self._test(ty)["type"], ty)
        r = self.client.post("/projects/1/manual-tests/", json={"type": "Static Load"})
        self.assertEqual(r.status_code, 422)

    def test_unknown_project_is_refused(self):
        r = self.client.post("/projects/999/manual-tests/",
                             json={"type": "ANSI Z97.1"})
        self.assertEqual(r.status_code, 404)


@unittest.skipIf(TestClient is None, "fastapi not installed in this environment")
class AttemptLifecycle(_Base):
    """Terminate and review — one route each, for every test type."""

    def setUp(self):
        super().setUp()
        t = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry"}).json()
        self.test_id = t["id"]
        self.attempt = self.start("/projects/1/manual-tests", t["id"])

    def test_an_attempt_starts_pending_and_unreviewed(self):
        a = self.attempt
        self.assertEqual(a["status"], "In Progress")
        self.assertEqual(a["test_result"], "Pending")
        self.assertIsNone(a["verdict_by"])
        # §6 — an unreviewed attempt has not answered the retest question.
        self.assertIsNone(a["retest_required"])

    def test_finish_records_the_operator_outcome_and_stays_pending(self):
        r = self.finish(self.attempt["id"], result=True, note="no entry achieved",
                        testing_continued="Stopped")
        self.assertEqual(r.status_code, 200, r.text)
        done = r.json()
        self.assertEqual(done["status"], "Completed")
        self.assertIs(done["result"], True)
        self.assertEqual(done["test_result"], "Pending")   # verdict is separate
        self.assertIsNotNone(done["testing_end_date"])

    def test_completion_without_an_outcome_is_refused(self):
        """Missing data is never a pass — completion is explicit."""
        r = self.finish(self.attempt["id"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("pass or fail", r.text)

    def test_abort_needs_only_a_reason(self):
        r = self.finish(self.attempt["id"], abort_reason="Equipment Fault")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "Aborted")

    def test_a_finished_attempt_cannot_be_finished_again(self):
        self.finish(self.attempt["id"], result=False)
        r = self.finish(self.attempt["id"], result=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("frozen", r.text)

    def test_verdict_records_reviewer_and_time_once(self):
        self.finish(self.attempt["id"], result=True)
        r = self.verdict(self.attempt["id"])
        self.assertEqual(r.status_code, 200, r.text)
        v = r.json()
        self.assertEqual(v["test_result"], "Pass")
        self.assertEqual(v["verdict_by"], "reviewer-1")
        self.assertIsNotNone(v["verdict_at"])
        self.assertIs(v["retest_required"], False)
        # Operator and reviewer are stored separately.
        self.assertEqual(v["operator_name"], "technician-1")

    def test_a_verdict_cannot_precede_termination(self):
        r = self.verdict(self.attempt["id"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("cannot precede termination", r.text)

    def test_a_second_verdict_is_refused(self):
        self.finish(self.attempt["id"], result=True)
        self.verdict(self.attempt["id"])
        r = self.verdict(self.attempt["id"], test_result="Fail",
                         verdict_by="reviewer-2")
        self.assertEqual(r.status_code, 409)
        self.assertIn("first verdict stands", r.text)

    def test_retest_required_must_be_stated(self):
        self.finish(self.attempt["id"], result=True)
        r = self.client.put(f"/test-results/{self.attempt['id']}/verdict",
                            json={"test_result": "Pass", "verdict_by": "r"})
        self.assertEqual(r.status_code, 422)

    def test_only_the_three_verdict_values_are_accepted(self):
        self.finish(self.attempt["id"], result=True)
        r = self.verdict(self.attempt["id"], test_result="Passed")  # wire spelling
        self.assertEqual(r.status_code, 422)

    def test_evidence_cannot_be_added_after_review(self):
        self.finish(self.attempt["id"], result=True)
        self.verdict(self.attempt["id"])
        r = self.client.post(f"/test-results/{self.attempt['id']}/photos",
                             files=_jpeg())
        self.assertEqual(r.status_code, 409)
        self.assertIn("requires a correction", r.text)

    def test_a_photo_attaches_to_the_attempt(self):
        r = self.client.post(f"/test-results/{self.attempt['id']}/photos",
                             files=_jpeg())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["shot_id"])


@unittest.skipIf(TestClient is None, "fastapi not installed in this environment")
class Impact(_Base):
    """Numbered impacts, each with its own value and its own photographs."""

    def setUp(self):
        super().setUp()
        t = self.client.post("/projects/1/impact-tests/",
                             json={"missile": "Large Missile D",
                                   "missile_weight": 9.0}).json()
        self.test_id = t["id"]
        self.attempt = self.start("/projects/1/impact-tests", t["id"])

    def shot(self, **body):
        body.setdefault("result", True)
        return self.client.post(f"/test-results/{self.attempt['id']}/shots",
                                json=body)

    def test_missile_metadata_is_optional(self):
        """The protocol fixes it; requiring it was retyping."""
        t = self.client.post("/projects/1/impact-tests/", json={}).json()
        self.assertIsNone(t["missile"])
        self.assertIsNone(t["missile_weight"])

    def test_an_impact_needs_only_pass_or_fail(self):
        r = self.shot()
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertIs(s["result"], True)
        self.assertIsNone(s["area"])
        self.assertIsNone(s["velocity"])

    def test_an_impact_without_an_outcome_is_refused(self):
        r = self.client.post(f"/test-results/{self.attempt['id']}/shots",
                             json={"area": 12.0})
        self.assertEqual(r.status_code, 422)

    def test_impacts_are_numbered_from_one_in_order(self):
        self.assertEqual([self.shot().json()["shot_number"] for _ in range(3)],
                         [1, 2, 3])

    def test_the_client_cannot_choose_the_number(self):
        self.assertEqual(self.shot(shot_number=7).json()["shot_number"], 1)

    def test_numbering_restarts_per_attempt(self):
        first = self.shot().json()["shot_number"]
        second_attempt = self.start("/projects/1/impact-tests", self.test_id)
        r = self.client.post(f"/test-results/{second_attempt['id']}/shots",
                             json={"result": True})
        self.assertEqual((first, r.json()["shot_number"]), (1, 1))

    def test_how_many_and_whether_each_passed(self):
        for res in (True, True, False):
            self.shot(result=res)
        listed = self.client.get(f"/test-results/{self.attempt['id']}/shots").json()
        self.assertEqual([s["result"] for s in listed], [True, True, False])
        self.assertEqual([s["shot_number"] for s in listed], [1, 2, 3])

    def test_a_photo_attaches_to_one_impact(self):
        shot = self.shot(result=False, note="corner cracked").json()
        r = self.client.post(f"/shots/{shot['id']}/photos",
                             files=_jpeg("impact-1.jpg"),
                             data={"note": "corner detail"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["shot_id"], shot["id"])
        listed = self.client.get(f"/test-results/{self.attempt['id']}/shots").json()
        self.assertEqual(len(listed[0]["photos"]), 1)
        self.assertEqual(listed[0]["photos"][0]["note"], "corner detail")

    def test_completion_requires_at_least_one_impact(self):
        self.client.post(f"/test-results/{self.attempt['id']}/photos", files=_jpeg())
        r = self.finish(self.attempt["id"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("at least one impact", r.text)

    def test_completion_requires_a_photograph(self):
        """Impact's evidence rule lives here, not in the outbound payload."""
        self.shot()
        r = self.finish(self.attempt["id"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("photograph", r.text)

    def test_a_per_impact_photo_satisfies_the_finish_requirement(self):
        shot = self.shot().json()
        self.client.post(f"/shots/{shot['id']}/photos", files=_jpeg())
        r = self.finish(self.attempt["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "Completed")

    def test_abort_skips_the_evidence_requirements(self):
        r = self.finish(self.attempt["id"], abort_reason="Specimen Failure")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "Aborted")

    def test_a_complete_impact_attempt(self):
        self.shot(result=True)
        self.shot(result=False, note="cracked")
        self.client.post(f"/test-results/{self.attempt['id']}/photos", files=_jpeg())
        self.assertEqual(self.finish(self.attempt["id"],
                                     testing_continued="Stopped").status_code, 200)
        v = self.verdict(self.attempt["id"], test_result="Fail",
                         retest_required=True)
        self.assertEqual(v.status_code, 200, v.text)
        self.assertEqual(v.json()["test_result"], "Fail")
        self.assertIs(v.json()["retest_required"], True)

    def test_only_an_impact_attempt_records_impacts(self):
        mt = self.client.post("/projects/1/manual-tests/",
                              json={"type": "ANSI Z97.1"}).json()
        other = self.start("/projects/1/manual-tests", mt["id"])
        r = self.client.post(f"/test-results/{other['id']}/shots",
                             json={"result": True})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
