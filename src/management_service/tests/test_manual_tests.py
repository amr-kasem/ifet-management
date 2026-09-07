"""Manual test capture — Impact, Forced Entry, ANSI Z97.1, through the real routes.

Delivery plan §4.5. These three are where the *phase* rules live — a verdict
cannot precede termination, a second verdict is refused, evidence cannot be added
after review — and none of those is observable from the models alone, which is
why this exercises the HTTP surface rather than the ORM.

Runs against the harness Postgres when `M2_DATABASE_URL` is set, SQLite in memory
otherwise. The rules here are application rules, not locking guarantees, so both
backends are meaningful — unlike the outbox suites, where SQLite proves nothing.
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
    return TestClient(main.app), Session, engine


@unittest.skipIf(TestClient is None, "fastapi not installed in this environment")
class ManualTestFlow(unittest.TestCase):
    """Forced Entry and ANSI Z97.1 — create, finish, review."""

    def setUp(self):
        self.client, self.Session, self.engine = _client_and_session()
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

    def _create(self, type_="Forced Entry", **over):
        body = {"type": type_, "required_option": "ASTM F588 Grade 40",
                "operator_name": "technician-1"}
        body.update(over)
        r = self.client.post("/projects/1/manual-tests/", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # -- create ------------------------------------------------------------

    def test_create_allocates_identity_and_starts_pending(self):
        t = self._create()
        self.assertTrue(t["labos_attempt_id"])
        self.assertEqual(t["attempt_number"], 1)
        self.assertEqual(t["status"], "In Progress")
        self.assertEqual(t["test_result"], "Pending")
        self.assertIsNone(t["verdict_by"])
        # §6 — an unreviewed attempt has not answered the retest question.
        self.assertIsNone(t["retest_required"])

    def test_attempt_numbers_increment_per_type(self):
        """Every attempt is retained — the same test may be attempted again."""
        self.assertEqual(self._create("Forced Entry")["attempt_number"], 1)
        self.assertEqual(self._create("Forced Entry")["attempt_number"], 2)
        # A different test type counts separately.
        self.assertEqual(self._create("ANSI Z97.1")["attempt_number"], 1)

    def test_both_types_are_accepted_and_nothing_else(self):
        for t in ("Forced Entry", "ANSI Z97.1"):
            with self.subTest(t):
                self.assertEqual(self._create(t)["type"], t)
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Static Load"})
        self.assertEqual(r.status_code, 422)

    def test_unknown_project_is_refused(self):
        r = self.client.post("/projects/999/manual-tests/", json={"type": "ANSI Z97.1"})
        self.assertEqual(r.status_code, 404)

    # -- finish ------------------------------------------------------------

    def test_finish_records_the_operator_outcome_and_stays_pending(self):
        t = self._create()
        r = self.client.put(f"/manual-tests/{t['id']}/finish",
                            json={"result": True, "note": "no entry achieved",
                                  "testing_continued": "Stopped"})
        self.assertEqual(r.status_code, 200, r.text)
        done = r.json()
        self.assertEqual(done["status"], "Completed")
        self.assertIs(done["result"], True)
        # The verdict belongs to the reviewer, not to this call.
        self.assertEqual(done["test_result"], "Pending")
        self.assertIsNotNone(done["testing_end_date"])

    def test_completion_without_an_outcome_is_refused(self):
        """Missing telemetry is never a pass — completion is explicit."""
        t = self._create()
        r = self.client.put(f"/manual-tests/{t['id']}/finish", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("pass or fail", r.text)

    def test_abort_needs_only_a_reason(self):
        t = self._create()
        r = self.client.put(f"/manual-tests/{t['id']}/finish",
                            json={"abort_reason": "Equipment Fault"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "Aborted")
        self.assertEqual(r.json()["abort_reason"], "Equipment Fault")

    def test_a_finished_attempt_cannot_be_finished_again(self):
        t = self._create()
        self.client.put(f"/manual-tests/{t['id']}/finish", json={"result": False})
        r = self.client.put(f"/manual-tests/{t['id']}/finish", json={"result": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("frozen", r.text)

    # -- verdict -----------------------------------------------------------

    def _verdict(self, tid, **over):
        body = {"test_result": "Pass", "verdict_by": "reviewer-1",
                "retest_required": False}
        body.update(over)
        return self.client.put(f"/manual-tests/{tid}/verdict", json=body)

    def test_verdict_records_reviewer_and_time_once(self):
        t = self._create()
        self.client.put(f"/manual-tests/{t['id']}/finish", json={"result": True})
        r = self._verdict(t["id"])
        self.assertEqual(r.status_code, 200, r.text)
        v = r.json()
        self.assertEqual(v["test_result"], "Pass")
        self.assertEqual(v["verdict_by"], "reviewer-1")
        self.assertIsNotNone(v["verdict_at"])
        self.assertIs(v["retest_required"], False)
        # Operator and reviewer are stored separately even when different people.
        self.assertEqual(v["operator_name"], "technician-1")

    def test_a_verdict_cannot_precede_termination(self):
        """§4 — evidence freezes on termination, then it is reviewed."""
        t = self._create()
        r = self._verdict(t["id"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("cannot precede termination", r.text)

    def test_a_second_verdict_is_refused(self):
        t = self._create()
        self.client.put(f"/manual-tests/{t['id']}/finish", json={"result": True})
        self._verdict(t["id"])
        r = self._verdict(t["id"], test_result="Fail", verdict_by="reviewer-2")
        self.assertEqual(r.status_code, 409)
        self.assertIn("first verdict stands", r.text)

    def test_retest_required_must_be_stated(self):
        """Not defaulted: an unchecked box is not a decision."""
        t = self._create()
        self.client.put(f"/manual-tests/{t['id']}/finish", json={"result": True})
        r = self.client.put(f"/manual-tests/{t['id']}/verdict",
                            json={"test_result": "Pass", "verdict_by": "r"})
        self.assertEqual(r.status_code, 422)

    def test_only_the_three_verdict_values_are_accepted(self):
        t = self._create()
        self.client.put(f"/manual-tests/{t['id']}/finish", json={"result": True})
        r = self._verdict(t["id"], test_result="Passed")   # their wire spelling
        self.assertEqual(r.status_code, 422)

    # -- photos ------------------------------------------------------------

    def _photo(self, path, name="fe.jpg"):
        return self.client.post(path, files={"file": (name, io.BytesIO(b"jpegbytes"),
                                                      "image/jpeg")})

    def test_photo_attaches_and_appears_on_the_attempt(self):
        t = self._create()
        r = self._photo(f"/manual-tests/{t['id']}/photos")
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/projects/1/manual-tests/").json()[0]
        self.assertEqual(len(got["photos"]), 1)

    def test_evidence_cannot_be_added_after_review(self):
        t = self._create()
        self.client.put(f"/manual-tests/{t['id']}/finish", json={"result": True})
        self._verdict(t["id"])
        r = self._photo(f"/manual-tests/{t['id']}/photos")
        self.assertEqual(r.status_code, 409)
        self.assertIn("requires a correction", r.text)

    # -- listing -----------------------------------------------------------

    def test_list_filters_by_type(self):
        self._create("Forced Entry")
        self._create("ANSI Z97.1")
        all_ = self.client.get("/projects/1/manual-tests/").json()
        ansi = self.client.get("/projects/1/manual-tests/?type=ANSI Z97.1").json()
        self.assertEqual(len(all_), 2)
        self.assertEqual([t["type"] for t in ansi], ["ANSI Z97.1"])


@unittest.skipIf(TestClient is None, "fastapi not installed in this environment")
class ImpactFlow(unittest.TestCase):
    """Missile impact — the capture path that never existed."""

    def setUp(self):
        self.client, self.Session, self.engine = _client_and_session()
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

    def _create(self, **over):
        body = {"missile": "Large Missile D", "missile_weight": 9.0,
                "operator_name": "technician-1"}
        body.update(over)
        r = self.client.post("/projects/1/impact-tests/", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _photo(self, tid):
        return self.client.post(f"/impact-tests/{tid}/photos",
                                files={"file": ("impact.jpg", io.BytesIO(b"jpg"),
                                                "image/jpeg")})

    def test_missile_metadata_is_optional(self):
        """The protocol fixes it; requiring it per attempt was retyping."""
        t = self.client.post("/projects/1/impact-tests/", json={}).json()
        self.assertIsNone(t["missile"])
        self.assertIsNone(t["missile_weight"])
        self.assertEqual(t["test_result"], "Pending")

    def test_a_shot_needs_only_pass_or_fail(self):
        t = self._create()
        r = self.client.post(f"/impact-tests/{t['id']}/shots", json={"result": True})
        self.assertEqual(r.status_code, 200, r.text)
        shot = r.json()
        self.assertIs(shot["result"], True)
        self.assertIsNone(shot["area"])
        self.assertIsNone(shot["velocity"])

    def test_a_shot_without_an_outcome_is_refused(self):
        t = self._create()
        r = self.client.post(f"/impact-tests/{t['id']}/shots", json={"area": 12.0})
        self.assertEqual(r.status_code, 422)

    def test_how_many_and_whether_each_passed(self):
        t = self._create()
        for res in (True, True, False):
            self.client.post(f"/impact-tests/{t['id']}/shots", json={"result": res})
        got = self.client.get("/projects/1/impact-tests/").json()[0]
        self.assertEqual([s["result"] for s in got["shots"]], [True, True, False])

    def test_completion_requires_at_least_one_impact(self):
        t = self._create()
        self._photo(t["id"])
        r = self.client.put(f"/impact-tests/{t['id']}/finish", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("at least one impact", r.text)

    def test_completion_requires_a_photograph(self):
        """Impact's evidence rule lives here, not in the outbound payload."""
        t = self._create()
        self.client.post(f"/impact-tests/{t['id']}/shots", json={"result": True})
        r = self.client.put(f"/impact-tests/{t['id']}/finish", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("photograph", r.text)

    def test_a_complete_impact_attempt(self):
        t = self._create()
        self.client.post(f"/impact-tests/{t['id']}/shots", json={"result": True})
        self.client.post(f"/impact-tests/{t['id']}/shots",
                         json={"result": False, "note": "cracked"})
        self._photo(t["id"])
        r = self.client.put(f"/impact-tests/{t['id']}/finish",
                            json={"testing_continued": "Stopped"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "Completed")
        self.assertEqual(r.json()["test_result"], "Pending")

        v = self.client.put(f"/impact-tests/{t['id']}/verdict",
                            json={"test_result": "Fail", "verdict_by": "reviewer-1",
                                  "retest_required": True})
        self.assertEqual(v.status_code, 200, v.text)
        self.assertEqual(v.json()["test_result"], "Fail")
        self.assertIs(v.json()["retest_required"], True)

    def test_abort_skips_the_evidence_requirements(self):
        t = self._create()
        r = self.client.put(f"/impact-tests/{t['id']}/finish",
                            json={"abort_reason": "Specimen Failure"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "Aborted")


if __name__ == "__main__":
    unittest.main()
