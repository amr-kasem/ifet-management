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

# A module-level fixture cannot be reused: BytesIO is consumed once. Call
# `_jpeg()` per request instead.


def _jpeg(name="evidence.jpg"):
    """A **real** JPEG, not `b"jpegbytes"`.

    The uploader downscales every photograph before sending it, so a fixture
    that is not a decodable image tests the refusal path and nothing else. This
    was literal `b"jpegbytes"` until the uploader existed, which is why
    "attachments park" looked like isolation rather than a missing capability.
    """
    return {"file": (name, io.BytesIO(_jpeg_bytes()), "image/jpeg")}


def _jpeg_bytes(size=(64, 48), colour=(180, 40, 40)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


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
    def _finish(self, attempt_id, result=True):
        """Terminate an attempt. **Required before a retest**, since 2026-09-08.

        A duplicate Start returns the open attempt rather than creating a second
        one — one test runs once at a time, so an operator's double-click must
        not become two certification records. An intentional retest therefore
        terminates the previous attempt first, which is the real workflow.
        """
        return self.client.put(
            f"/test-results/{attempt_id}/finish",
            json={"result": result, "testing_continued": "Stopped"})

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
        # Terminate before retesting. A Start while attempt 1 is open returns
        # attempt 1 — see `_finish`.
        self._finish(a1["id"], result=False)
        a2 = self.start("/projects/1/manual-tests", t["id"])
        self.assertEqual((a1["trial_number"], a2["trial_number"]), (1, 2))
        self.assertEqual(a1["labos_test_id"], a2["labos_test_id"])
        self.assertNotEqual(a1["labos_attempt_id"], a2["labos_attempt_id"])

    def test_attempts_are_listed_under_their_test(self):
        t = self._test()
        first = self.start("/projects/1/manual-tests", t["id"])
        self._finish(first["id"], result=False)
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

    def test_listing_tests_includes_their_attempts(self):
        t = self._test()
        self.start("/projects/1/manual-tests", t["id"])
        listed = self.client.get("/projects/1/manual-tests/").json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(len(listed[0]["trials"]), 1)
        self.assertEqual(listed[0]["required_option"], "ASTM F588 Grade 40")

    def test_listing_filters_by_type(self):
        self._test("Forced Entry")
        self._test("ANSI Z97.1")
        self.assertEqual(len(self.client.get("/projects/1/manual-tests/").json()), 2)
        ansi = self.client.get("/projects/1/manual-tests/?type=ANSI Z97.1").json()
        self.assertEqual([t["type"] for t in ansi], ["ANSI Z97.1"])

    def test_the_project_embeds_manual_tests_like_every_other_type(self):
        """A UI reading the project once must not silently miss these.

        `ProjectSchema` already embedded static, cyclic, water and impact. It
        did not embed manual tests until 2026-09-08, so Forced Entry and ANSI
        were the only test types invisible from the project payload.
        """
        t = self._test()
        self.start("/projects/1/manual-tests", t["id"])
        project = self.client.get("/devices/1/projects/").json()[0]
        self.assertEqual([m["type"] for m in project["manual_tests"]],
                         ["Forced Entry"])
        self.assertEqual(len(project["manual_tests"][0]["trials"]), 1)
        # and the pre-existing embeds still work
        for key in ("static_tests", "cyclic_tests", "missile_impact_tests",
                    "infiltration_tests"):
            self.assertIn(key, project)

    def test_listing_an_unknown_project_is_refused(self):
        self.assertEqual(self.client.get("/projects/999/manual-tests/").status_code, 404)


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

    def test_the_attempt_is_readable_on_the_shared_route(self):
        got = self.client.get(f"/test-results/{self.attempt['id']}")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["id"], self.attempt["id"])
        self.assertEqual(got.json()["trial_number"], 1)

    def test_the_legacy_update_route_amends_notes_and_nothing_else(self):
        """`PUT /test-results/{id}` predates this work and must stay narrow.

        MANUAL_TESTS_API.md tells the UI developer it amends `note` only and is
        **not** a way to edit a reviewed attempt. That claim was untested, and a
        route that silently accepted a verdict would make the documentation
        wrong in the most expensive direction.
        """
        self.finish(self.attempt["id"], result=True)
        self.verdict(self.attempt["id"], test_result="Fail", retest_required=True)

        # Form-encoded, not JSON: it predates the JSON routes and takes
        # `note` and `image` as multipart form fields.
        r = self.client.put(f"/test-results/{self.attempt['id']}",
                            data={"note": "amended after the fact"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["note"], "amended after the fact")

        # Its response schema exposes only id/trial_number/note/image_path/
        # result — it cannot even return a verdict — so the verdict is checked
        # through the attempt route, which can.
        attempt = self.client.get(
            f"/projects/1/manual-tests/{self.test_id}/trials").json()[0]
        self.assertEqual(attempt["note"], "amended after the fact")
        self.assertEqual(attempt["test_result"], "Fail")
        self.assertEqual(attempt["verdict_by"], "reviewer-1")
        self.assertIs(attempt["retest_required"], True)

    def test_a_photo_attaches_to_the_attempt(self):
        r = self.client.post(f"/test-results/{self.attempt['id']}/photos",
                             files=_jpeg())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["shot_id"])


@unittest.skipIf(TestClient is None, "fastapi not installed in this environment")
class Impact(_Base):
    """One attempt per impact, each with its own outcome and photographs.

    **Reshaped 2026-09-08** (delivery plan §4.5a). An impact test was one
    attempt holding a sequence of shots; the product owner respecified it as one
    or more attempts, each being exactly one impact. The sequence is still
    impact 1, 2, 3 — it is now made of attempts.
    """

    def setUp(self):
        super().setUp()
        # A LabOS-only impact test, so the family is the operator's to set —
        # there is no bound section owning it. Classification and target
        # velocity are supplied here because an attempt cannot *complete*
        # without them since 2026-09-10; they are still optional at creation,
        # which `test_missile_metadata_is_optional` pins.
        t = self.client.post("/projects/1/impact-tests/",
                             json={"missile": "Large Missile D",
                                   "missile_weight": 9.0,
                                   "impact_family": "LMI",
                                   "impact_level": "D",
                                   "target_velocity": 50.0}).json()
        self.test_id = t["id"]
        self.attempt = self.start("/projects/1/impact-tests", t["id"])

    def shot(self, **body):
        body.setdefault("result", True)
        return self.client.post(f"/test-results/{self.attempt['id']}/shots",
                                json=body)

    def impact(self, result=True, photos=1):
        """One whole impact: its own attempt, its outcome, its evidence.

        Returns `(attempt, shot)`. Terminates the attempt, because the next
        impact is the next attempt and a test runs one attempt at a time.
        """
        attempt = (self.attempt if not getattr(self, "_used_first", False)
                   else self.start("/projects/1/impact-tests", self.test_id))
        self._used_first = True
        r = self.client.post(f"/test-results/{attempt['id']}/shots",
                             json={"result": result})
        self.assertEqual(r.status_code, 200, r.text)
        shot = r.json()
        for i in range(photos):
            p = self.client.post(f"/shots/{shot['id']}/photos",
                                 files=_jpeg(f"impact{shot['shot_number']}-{i}.jpg"))
            self.assertEqual(p.status_code, 200, p.text)
        self._finish(attempt["id"], result=result)
        return attempt, shot

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
        """And the number is the attempt's, mirrored onto its impact."""
        numbers = []
        for _ in range(3):
            attempt, shot = self.impact()
            numbers.append((attempt["trial_number"], shot["shot_number"]))
        self.assertEqual(numbers, [(1, 1), (2, 2), (3, 3)])

    def test_the_client_cannot_choose_the_number(self):
        self.assertEqual(self.shot(shot_number=7).json()["shot_number"], 1)

    def test_numbering_does_not_restart(self):
        """It used to restart per attempt. Now the attempt *is* the impact.

        The old behaviour would give every impact `shot_number = 1`, and the
        number is published — the JSON emits it as which impact this is.
        """
        _first, shot1 = self.impact()
        second, shot2 = self.impact()
        self.assertEqual((shot1["shot_number"], shot2["shot_number"]), (1, 2))
        self.assertEqual(second["trial_number"], shot2["shot_number"])

    def test_one_attempt_refuses_a_second_impact(self):
        """The invariant, and it is the constraint reporting itself in words."""
        self.assertEqual(self.shot().status_code, 200)
        r = self.shot(result=False)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("One attempt is one impact", r.text)

    def test_the_attempt_outcome_follows_its_impact(self):
        """No `result` on finish: an impact attempt's outcome is its impact's."""
        shot = self.shot(result=False).json()
        self.client.post(f"/shots/{shot['id']}/photos", files=_jpeg())
        r = self.client.put(f"/test-results/{self.attempt['id']}/finish",
                            json={"testing_continued": "Stopped"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["result"], False)

    def test_how_many_and_whether_each_passed(self):
        """Three impacts are three attempts, each carrying its own outcome."""
        results = [self.impact(result=r)[1]["result"] for r in (True, True, False)]
        self.assertEqual(results, [True, True, False])

        listed = self.client.get(f"/projects/1/impact-tests/").json()
        attempts = next(t for t in listed if t["id"] == self.test_id)["trials"]
        self.assertEqual([a["trial_number"] for a in attempts], [1, 2, 3])
        self.assertEqual([a["result"] for a in attempts], [True, True, False])

    def test_one_impact_can_carry_several_photographs(self):
        """"A few photos" per impact — the relationship is one-to-many.

        Asserted explicitly because the obvious wrong implementation, a single
        `photo_path` column on the shot, would pass every other test here.
        """
        shot = self.shot(result=False, note="corner cracked").json()
        for name, note in (("wide.jpg", "wide shot"),
                           ("corner.jpg", "corner detail"),
                           ("interior.jpg", "interior face")):
            r = self.client.post(f"/shots/{shot['id']}/photos",
                                 files=_jpeg(name), data={"note": note})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["shot_id"], shot["id"])

        listed = self.client.get(f"/test-results/{self.attempt['id']}/shots").json()
        self.assertEqual(len(listed), 1)
        self.assertEqual([p["note"] for p in listed[0]["photos"]],
                         ["wide shot", "corner detail", "interior face"])

    def test_photographs_stay_with_their_own_impact(self):
        """Three impacts in three attempts, different photograph counts each."""
        seen = []
        for result, n in ((True, 1), (False, 3), (True, 2)):
            attempt, shot = self.impact(result=result, photos=n)
            listed = self.client.get(
                f"/test-results/{attempt['id']}/shots").json()
            self.assertEqual(len(listed), 1, "one attempt, one impact")
            seen.append((listed[0]["shot_number"], len(listed[0]["photos"])))
        self.assertEqual(seen, [(1, 1), (2, 3), (3, 2)])

    def test_attempt_photos_and_impact_photos_do_not_mix(self):
        """An attempt-level photograph has shot_id NULL and is not listed under
        any impact — so the UI never renders the same photograph twice."""
        shot = self.shot().json()
        self.client.post(f"/shots/{shot['id']}/photos", files=_jpeg("impact.jpg"))
        self.client.post(f"/test-results/{self.attempt['id']}/photos",
                         files=_jpeg("specimen-before.jpg"))

        listed = self.client.get(f"/test-results/{self.attempt['id']}/shots").json()
        self.assertEqual([p["filename"] for p in listed[0]["photos"]],
                         ["impact.jpg"])

        attempt = self.client.get(f"/projects/1/impact-tests/{self.test_id}/trials").json()[0]
        # The attempt carries both: its own, plus the per-impact one, because a
        # per-impact photograph is still evidence of the attempt.
        by_shot = {p["filename"]: p["shot_id"] for p in attempt["photos"]}
        self.assertIsNone(by_shot["specimen-before.jpg"])
        self.assertEqual(by_shot["impact.jpg"], shot["id"])

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

    def test_completion_requires_its_one_impact(self):
        """§4.5a: exactly one, so an attempt with none has nothing to report."""
        self.client.post(f"/test-results/{self.attempt['id']}/photos", files=_jpeg())
        r = self.finish(self.attempt["id"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("exactly one impact", r.text)
        self.assertIn("has 0", r.text)

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

    def test_listing_impact_tests_includes_their_attempts(self):
        listed = self.client.get("/projects/1/impact-tests/").json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["missile"], "Large Missile D")
        self.assertEqual(len(listed[0]["trials"]), 1)
        self.assertFalse(listed[0]["finished"])

    def test_finishing_an_impact_test_blocks_further_attempts(self):
        r = self.client.put(f"/projects/1/impact-tests/{self.test_id}/finish")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["finished"])
        again = self.client.post(f"/projects/1/impact-tests/{self.test_id}/trials",
                                 json={})
        self.assertEqual(again.status_code, 400)
        self.assertIn("finished", again.text)

    def test_only_an_impact_attempt_records_impacts(self):
        # The impact attempt from setUp is still open, and one rig runs one test
        # at a time — so starting a second test on the same rig is refused. That
        # is the rule under test elsewhere; here, terminate first so this test
        # is about shots on a non-impact attempt and nothing else.
        first_shot = self.shot()
        self.client.post(f"/shots/{first_shot.json()['id']}/photos", files=_jpeg())
        self._finish(self.attempt["id"], result=True)

        mt = self.client.post("/projects/1/manual-tests/",
                              json={"type": "ANSI Z97.1"}).json()
        other = self.start("/projects/1/manual-tests", mt["id"])
        r = self.client.post(f"/test-results/{other['id']}/shots",
                             json={"result": True})
        self.assertEqual(r.status_code, 400)


class ImpactClassificationOwnership(_Base):
    """Airtable owns the impact family; LabOS owns the level and the velocity.

    `Requirement Code` already says IMPACT_SMI or IMPACT_LMI, so letting an
    operator choose the family would create a second source of truth that can
    contradict the first. These tests pin that they cannot.

    The authority is **this test's own `airtable_section_id`**, not its
    project's: a project can be Airtable-bound while a test added to it is not.

    `Impact Classification` is derived output only. There is no column for it
    and no API field that sets it, so a classification disagreeing with the
    requirement is unrepresentable rather than merely refused.
    """

    URL = "/projects/1/impact-tests/"

    def make(self, **body):
        return self.client.post(self.URL, json=body)

    def labos_only(self, **over):
        body = {"impact_family": "LMI", "impact_level": "D",
                "target_velocity": 50.0}
        body.update(over)
        r = self.make(**body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _mirror_section(self, record_id, code):
        """A mirrored Protocol Section the create route can resolve."""
        from app.airtable import requirements as req
        from app.airtable.mirror import AtMirrorSection
        s = self.Session()
        try:
            s.add(AtMirrorSection(record_id=record_id,
                                  protocol_record_id="recPROT000000001",
                                  section_name=code, requirement_code=code,
                                  requirement_kind=req.KIND_BY_CODE[code],
                                  applicability="Required",
                                  required_value=3.0,
                                  required_unit="impacts"))
            s.commit()
        finally:
            s.close()

    def bound(self, section="recSEC000000001", code="IMPACT_LMI", **over):
        """A test bound to a Protocol Section, as the UI creates one.

        `MANUAL_TESTS_API.md` section 7 tells the UI to send `airtable_*`
        "from the mirror" for an Airtable-linked job, so this is a real
        product path and not only what `importer.bind` produces.
        """
        if code is not None:
            self._mirror_section(section, code)
        body = {"airtable_section_id": section}
        body.update(over)
        return self.make(**body)

    # -- the vocabulary lives in Pydantic, not the database ---------------

    def test_an_unknown_family_is_refused_by_the_schema(self):
        self.assertEqual(422, self.make(impact_family="LARGE").status_code)

    def test_an_unknown_level_is_refused_by_the_schema(self):
        self.assertEqual(
            422, self.make(impact_family="LMI", impact_level="F").status_code)

    def test_the_accepted_families_and_levels_are_exactly_these(self):
        from app.data.schema import IMPACT_FAMILIES, IMPACT_LEVELS
        self.assertEqual(("SMI", "LMI"), IMPACT_FAMILIES)
        self.assertEqual(("D", "E"), IMPACT_LEVELS)

    # -- the binding is resource-level, and it is the authority -----------

    def test_a_bound_test_refuses_an_operator_supplied_family(self):
        r = self.bound(impact_family="SMI")
        self.assertEqual(400, r.status_code)
        self.assertIn("owned by the bound Airtable requirement code",
                      r.json()["detail"])

    def test_a_labos_only_test_may_set_its_own_family(self):
        self.assertEqual("SMI", self.labos_only(
            impact_family="SMI", impact_level=None)["impact_family"])

    def test_the_authority_is_the_tests_own_section_not_its_project(self):
        """The project is Airtable-bound in this fixture either way; what
        decides is whether *this test* carries a section id."""
        self.assertEqual(400, self.bound(impact_family="LMI").status_code)
        self.assertEqual(200, self.make(impact_family="LMI").status_code)

    # -- SMI never carries a level ----------------------------------------

    def test_creating_smi_with_a_level_is_refused(self):
        r = self.make(impact_family="SMI", impact_level="D")
        self.assertEqual(400, r.status_code)
        self.assertIn("LMI only", r.json()["detail"])

    def test_patching_a_level_onto_smi_is_refused(self):
        t = self.labos_only(impact_family="SMI", impact_level=None)
        r = self.client.patch(f"{self.URL}{t['id']}", json={"impact_level": "D"})
        self.assertEqual(400, r.status_code)
        self.assertIn("LMI only", r.json()["detail"])

    def test_a_level_before_the_family_is_known_is_refused(self):
        """The DB CHECK permits this deliberately — the family may not be
        known yet. The route is what refuses it, which is the middle tier
        doing its job."""
        r = self.make(impact_level="D")
        self.assertEqual(400, r.status_code)
        self.assertIn("before the impact family is known", r.json()["detail"])

    def test_patch_cannot_set_the_family_on_a_bound_test(self):
        """Refused with a sentence, not ignored — a silent no-op would read
        as acceptance."""
        self._mirror_section("recSEC_BOUND1", "IMPACT_LMI")
        t = self.client.post(self.URL,
                             json={"airtable_section_id": "recSEC_BOUND1"}).json()
        r = self.client.patch(f"{self.URL}{t['id']}",
                              json={"impact_family": "SMI"})
        self.assertEqual(400, r.status_code)
        self.assertIn("owned by the bound Airtable requirement code",
                      r.json()["detail"])

    # -- the derived value -------------------------------------------------

    def test_the_classification_is_derived_for_each_case(self):
        for family, level, expected in (("SMI", None, "SMI"),
                                        ("LMI", "D", "LMI Level D"),
                                        ("LMI", "E", "LMI Level E")):
            with self.subTest(family=family, level=level):
                t = self.labos_only(impact_family=family, impact_level=level)
                self.assertEqual(expected, t["impact_classification"])

    def test_an_lmi_without_a_level_has_no_classification_yet(self):
        t = self.labos_only(impact_level=None)
        self.assertIsNone(t["impact_classification"])

    def test_the_classification_is_not_an_api_input(self):
        """Sending it changes nothing: it is not a field on the create schema
        and there is nowhere for it to land."""
        r = self.make(impact_family="SMI", impact_classification="LMI Level E")
        self.assertEqual(200, r.status_code)
        self.assertEqual("SMI", r.json()["impact_classification"])

    # -- optional at creation, required at completion ---------------------

    def test_both_are_optional_when_the_test_is_created(self):
        t = self.make().json()
        self.assertIsNone(t["impact_family"])
        self.assertIsNone(t["target_velocity"])

    def _one_impact(self, test_id):
        attempt = self.start("/projects/1/impact-tests", test_id)
        shot = self.client.post(f"/test-results/{attempt['id']}/shots",
                                json={"result": True}).json()
        self.client.post(f"/shots/{shot['id']}/photos", files=_jpeg())
        return attempt

    def test_an_lmi_without_a_level_cannot_complete(self):
        t = self.labos_only(impact_level=None)
        attempt = self._one_impact(t["id"])
        r = self.finish(attempt["id"], result=True)
        self.assertEqual(400, r.status_code)
        self.assertIn("needs its level", r.json()["detail"])

    def test_a_test_without_a_target_velocity_cannot_complete(self):
        t = self.labos_only(target_velocity=None)
        attempt = self._one_impact(t["id"])
        r = self.finish(attempt["id"], result=True)
        self.assertEqual(400, r.status_code)
        self.assertIn("target velocity", r.json()["detail"])

    def test_patch_supplies_them_and_then_it_completes(self):
        """The reason the route exists: an LMI test created with neither the
        level nor the velocity cannot complete, and PATCH is the supported way
        to supply both before it does."""
        t = self.labos_only(impact_level=None, target_velocity=None)
        self.assertIsNone(t["impact_classification"])

        r = self.client.patch(f"{self.URL}{t['id']}",
                              json={"impact_level": "E", "target_velocity": 55.0})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("LMI Level E", r.json()["impact_classification"])
        self.assertEqual(55.0, r.json()["target_velocity"])

        attempt = self._one_impact(t["id"])
        self.assertEqual(200, self.finish(attempt["id"], result=True).status_code)

    def test_an_abort_needs_neither(self):
        """An abort produced no result, so it claims nothing to be wrong."""
        t = self.labos_only(impact_level=None, target_velocity=None)
        attempt = self.start("/projects/1/impact-tests", t["id"])
        r = self.finish(attempt["id"], abort_reason="Equipment Fault")
        self.assertEqual(200, r.status_code, r.text)

    # -- immutability: completed freezes the level, aborted does not ------

    def test_a_completed_attempt_freezes_the_level(self):
        t = self.labos_only()
        attempt = self._one_impact(t["id"])
        self.assertEqual(200, self.finish(attempt["id"], result=True).status_code)
        r = self.client.patch(f"{self.URL}{t['id']}", json={"impact_level": "E"})
        self.assertEqual(409, r.status_code)
        self.assertIn("completed attempt", r.json()["detail"])

    def test_an_aborted_attempt_does_not_freeze_the_level(self):
        """**The distinction that matters for the level.** An abort recorded
        no result, so nothing has been claimed yet. (The *family* is stricter
        — see ALabosOnlyTestCanAcquireItsFamily.)"""
        t = self.labos_only()
        attempt = self.start("/projects/1/impact-tests", t["id"])
        self.assertEqual(200, self.finish(attempt["id"],
                                          abort_reason="Equipment Fault").status_code)
        r = self.client.patch(f"{self.URL}{t['id']}", json={"impact_level": "E"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("LMI Level E", r.json()["impact_classification"])

    def test_an_abort_then_a_completion_does_freeze(self):
        """The abort is not what freezes the level; the completion is."""
        t = self.labos_only()
        first = self.start("/projects/1/impact-tests", t["id"])
        self.finish(first["id"], abort_reason="Equipment Fault")
        second = self._one_impact(t["id"])
        self.finish(second["id"], result=True)
        self.assertEqual(409, self.client.patch(
            f"{self.URL}{t['id']}", json={"impact_level": "E"}).status_code)



class BoundCreateResolvesTheFamilyServerSide(_Base):
    """A bound test created through the generic POST must not end up NULL.

    This is a documented product path — `MANUAL_TESTS_API.md` section 7 tells
    the UI to send `airtable_*` "from the mirror" — so the route resolves the
    family the same way `importer.bind` does, through the one shared
    `IMPACT_FAMILY_BY_CODE` mapping rather than a second copy of the rule.
    """

    URL = "/projects/1/impact-tests/"

    def section(self, record_id, code):
        from app.airtable import requirements as req
        from app.airtable.mirror import AtMirrorSection
        s = self.Session()
        try:
            s.add(AtMirrorSection(record_id=record_id,
                                  protocol_record_id="recPROT000000001",
                                  section_name=code, requirement_code=code,
                                  requirement_kind=req.KIND_BY_CODE[code],
                                  applicability="Required",
                                  required_value=3.0,
                                  required_unit=("impacts" if code.startswith("IMPACT")
                                                 else None),
                                  required_option=("Class A" if code == "ANSI_IMPACT"
                                                   else None)))
            s.commit()
        finally:
            s.close()

    def create(self, **body):
        return self.client.post(self.URL, json=body)

    def test_an_smi_section_stores_smi(self):
        self.section("recSEC_SMI", "IMPACT_SMI")
        r = self.create(airtable_section_id="recSEC_SMI")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("SMI", r.json()["impact_family"])
        self.assertEqual("SMI", r.json()["impact_classification"])

    def test_an_lmi_section_stores_lmi(self):
        self.section("recSEC_LMI", "IMPACT_LMI")
        r = self.create(airtable_section_id="recSEC_LMI")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("LMI", r.json()["impact_family"])
        # The level is still the operator's, so there is no classification yet.
        self.assertIsNone(r.json()["impact_level"])
        self.assertIsNone(r.json()["impact_classification"])

    def test_a_bound_create_never_leaves_the_family_null(self):
        """The defect this closes: a bound test that can never finish."""
        for code in ("IMPACT_SMI", "IMPACT_LMI"):
            with self.subTest(code=code):
                self.section(f"recSEC_{code}", code)
                r = self.create(airtable_section_id=f"recSEC_{code}")
                self.assertIsNotNone(r.json()["impact_family"])

    def test_a_client_cannot_override_the_bound_family(self):
        self.section("recSEC_SMI2", "IMPACT_SMI")
        r = self.create(airtable_section_id="recSEC_SMI2", impact_family="LMI")
        self.assertEqual(400, r.status_code)
        self.assertIn("cannot be supplied or overridden", r.json()["detail"])

    def test_even_an_agreeing_family_is_refused(self):
        """Refused because it is not the client's to send, not because it
        disagrees — agreeing today and drifting tomorrow is the failure."""
        self.section("recSEC_SMI3", "IMPACT_SMI")
        r = self.create(airtable_section_id="recSEC_SMI3", impact_family="SMI")
        self.assertEqual(400, r.status_code)

    def test_an_unknown_section_is_refused(self):
        r = self.create(airtable_section_id="recSEC_NOT_IN_MIRROR")
        self.assertEqual(400, r.status_code)
        self.assertIn("not in the mirror", r.json()["detail"])

    def test_a_non_impact_section_cannot_be_bound_to_an_impact_test(self):
        self.section("recSEC_ANSI", "ANSI_IMPACT")
        r = self.create(airtable_section_id="recSEC_ANSI")
        self.assertEqual(400, r.status_code)
        self.assertIn("not an impact requirement", r.json()["detail"])

    def test_a_static_section_cannot_be_bound_either(self):
        self.section("recSEC_STATIC", "STATIC_PRESSURE")
        self.assertEqual(400,
                         self.create(airtable_section_id="recSEC_STATIC").status_code)

    def test_the_route_and_the_importer_share_one_mapping(self):
        """Not two copies of the SMI/LMI rule that can drift."""
        from app.airtable.importer import IMPACT_FAMILY_BY_CODE
        from app import main
        self.assertIs(IMPACT_FAMILY_BY_CODE,
                      main.importer.IMPACT_FAMILY_BY_CODE)
        self.assertEqual({"IMPACT_SMI": "SMI", "IMPACT_LMI": "LMI"},
                         IMPACT_FAMILY_BY_CODE)


class ALabosOnlyTestCanAcquireItsFamily(_Base):
    """The lifecycle defect: `{}` created, then never finishable.

    `impact_family` was write-never through PATCH, and a LabOS-only test has
    no requirement code to supply one at creation. A test made with `{}` could
    therefore never be classified and never complete an attempt.
    """

    URL = "/projects/1/impact-tests/"

    def blank(self):
        r = self.client.post(self.URL, json={})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def patch(self, test_id, **body):
        return self.client.patch(f"{self.URL}{test_id}", json=body)

    def _one_impact(self, test_id):
        attempt = self.start("/projects/1/impact-tests", test_id)
        shot = self.client.post(f"/test-results/{attempt['id']}/shots",
                                json={"result": True}).json()
        self.client.post(f"/shots/{shot['id']}/photos", files=_jpeg())
        return attempt

    def test_a_blank_test_can_be_classified_and_then_finished(self):
        """End to end, and the whole point of the correction."""
        t = self.blank()
        self.assertIsNone(t["impact_family"])

        r = self.patch(t["id"], impact_family="SMI", target_velocity=130.0)
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("SMI", r.json()["impact_classification"])

        attempt = self._one_impact(t["id"])
        self.assertEqual(200, self.finish(attempt["id"], result=True).status_code)

    def test_it_can_become_lmi_with_a_level_in_one_request(self):
        t = self.blank()
        r = self.patch(t["id"], impact_family="LMI", impact_level="E",
                       target_velocity=55.0)
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("LMI Level E", r.json()["impact_classification"])

    def test_the_family_is_fixed_once_any_attempt_exists(self):
        t = self.blank()
        self.patch(t["id"], impact_family="LMI", impact_level="D",
                   target_velocity=50.0)
        self.start("/projects/1/impact-tests", t["id"])
        r = self.patch(t["id"], impact_family="SMI")
        self.assertEqual(409, r.status_code)
        self.assertIn("execution has begun", r.json()["detail"])

    def test_even_an_aborted_attempt_fixes_the_family(self):
        """**Stricter than the level and velocity rule, deliberately.** An
        abort still ran against a missile; relabelling the test afterwards
        would change what that attempt meant."""
        t = self.blank()
        self.patch(t["id"], impact_family="LMI", impact_level="D",
                   target_velocity=50.0)
        attempt = self.start("/projects/1/impact-tests", t["id"])
        self.finish(attempt["id"], abort_reason="Equipment Fault")

        self.assertEqual(409, self.patch(t["id"], impact_family="SMI").status_code)
        # ...while the level and the velocity are still editable, because no
        # attempt has completed.
        self.assertEqual(200, self.patch(t["id"], impact_level="E",
                                         target_velocity=55.0).status_code)

    def test_an_unclassified_test_cannot_start_an_attempt(self):
        """**The strand, closed at the near end.**

        The family is fixed by the first attempt, but the classification is
        only *demanded* when an attempt completes. Between those two moments a
        test started without a family had no way forward: PATCH answered 409
        (execution has begun), `/finish` answered 400 (no classification), and
        abort was the only exit. Refusing the start removes the state instead
        of adding a fourth escape from it.
        """
        t = self.blank()
        r = self.client.post(f"{self.URL}{t['id']}/trials",
                             json={"operator_name": "technician-1"})
        self.assertEqual(400, r.status_code, r.text)
        self.assertIn("no missile classification", r.json()["detail"])

        # ...and it starts perfectly well once the family is there.
        self.patch(t["id"], impact_family="SMI", target_velocity=130.0)
        self.start("/projects/1/impact-tests", t["id"])

    def test_lmi_without_a_level_may_still_start(self):
        """Only the *family* is required up front, not the level.

        An LMI test with no level is not stranded: aborts do not count as
        completed attempts, so PATCH can still supply the level while the
        attempt is open. Refusing this start would move a deadline rather than
        close a gap.
        """
        t = self.blank()
        self.patch(t["id"], impact_family="LMI", target_velocity=50.0)
        attempt = self.start("/projects/1/impact-tests", t["id"])
        self.assertEqual(200, self.patch(t["id"], impact_level="D").status_code)
        self.assertEqual(200, self.finish(attempt["id"],
                                          abort_reason="Equipment Fault").status_code)

    def test_a_test_already_stranded_can_still_reach_its_open_attempt(self):
        """Recovery for rows that predate the guard.

        A test stranded by the old behaviour still has an open attempt, and the
        operator's only exit is to abort it — which needs its id. So the guard
        is skipped when an attempt is already open: Start stays idempotent and
        hands back the attempt rather than a 400 that hides it.
        """
        from app.data.models import MissileImpactTest
        t = self.blank()
        self.patch(t["id"], impact_family="SMI", target_velocity=130.0)
        attempt = self.start("/projects/1/impact-tests", t["id"])

        # Reproduce the legacy state: an open attempt on a family-less test.
        s = self.Session()
        s.query(MissileImpactTest).filter_by(id=t["id"]).update(
            {"impact_family": None})
        s.commit()
        s.close()

        r = self.client.post(f"{self.URL}{t['id']}/trials",
                             json={"operator_name": "technician-1"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(attempt["id"], r.json()["id"])
        self.assertEqual(200, self.finish(attempt["id"],
                                          abort_reason="Unclassified test").status_code)

    def test_switching_to_smi_while_a_level_is_stored_is_refused(self):
        t = self.blank()
        self.patch(t["id"], impact_family="LMI", impact_level="D")
        r = self.patch(t["id"], impact_family="SMI")
        self.assertEqual(400, r.status_code)
        self.assertIn("applies to LMI only", r.json()["detail"])

    def test_switching_to_smi_and_clearing_the_level_together_works(self):
        t = self.blank()
        self.patch(t["id"], impact_family="LMI", impact_level="D")
        r = self.patch(t["id"], impact_family="SMI", impact_level=None)
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("SMI", r.json()["impact_classification"])


class TheImpactClassificationIsDerivedFromTheModel(_Base):
    """`Impact Classification` is computed, not stored.

    Tested against the model rather than through a route on purpose: there is
    no column for it and no API field that sets it, so the derivation is the
    whole of its definition. A route test would prove the route, not the rule.
    """

    def test_smi_derives_smi(self):
        from app.data.models import MissileImpactTest
        self.assertEqual(
            "SMI", MissileImpactTest(impact_family="SMI").impact_classification)

    def test_lmi_with_a_level_derives_the_level(self):
        from app.data.models import MissileImpactTest
        for level, expected in (("D", "LMI Level D"), ("E", "LMI Level E")):
            with self.subTest(level=level):
                t = MissileImpactTest(impact_family="LMI", impact_level=level)
                self.assertEqual(expected, t.impact_classification)

    def test_lmi_without_a_level_derives_nothing_yet(self):
        from app.data.models import MissileImpactTest
        self.assertIsNone(
            MissileImpactTest(impact_family="LMI").impact_classification)

    def test_an_unclassified_test_derives_nothing(self):
        """The 39 historical rows: no family, no classification, still legal."""
        from app.data.models import MissileImpactTest
        self.assertIsNone(
            MissileImpactTest(missile="2x4 Lumber").impact_classification)

    def test_a_level_without_a_family_derives_nothing(self):
        """Belt and braces: the level alone never invents a family."""
        from app.data.models import MissileImpactTest
        self.assertIsNone(
            MissileImpactTest(impact_level="D").impact_classification)


class TheDatabaseRefusesSmiWithALevel(_Base):
    """The one structural invariant, checked at the database and not only in
    the route — so bypassing the API cannot produce the state either."""

    def test_the_check_constraint_exists_and_is_named(self):
        from app.data.models import MissileImpactTest
        names = [c.name for c in MissileImpactTest.__table__.constraints
                 if c.name]
        self.assertIn("ck_missile_impact_tests_smi_has_no_level", names)

    def test_smi_with_a_level_is_refused_by_the_database(self):
        import sqlalchemy as sa
        from app.data.models import MissileImpactTest
        s = self.Session()
        try:
            s.add(MissileImpactTest(project_id=1, finished=False,
                                    impact_family="SMI", impact_level="D"))
            with self.assertRaises(sa.exc.IntegrityError):
                s.commit()
        finally:
            s.rollback()
            s.close()

    def test_lmi_with_a_level_is_accepted(self):
        from app.data.models import MissileImpactTest
        s = self.Session()
        try:
            s.add(MissileImpactTest(project_id=1, finished=False,
                                    impact_family="LMI", impact_level="D"))
            s.commit()
        finally:
            s.close()

    def test_a_historical_row_with_everything_null_is_still_valid(self):
        """The 39 pre-existing tests are not backfilled and must stay legal."""
        from app.data.models import MissileImpactTest
        s = self.Session()
        try:
            s.add(MissileImpactTest(project_id=1, finished=False,
                                    missile="2x4 Lumber"))
            s.commit()
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
