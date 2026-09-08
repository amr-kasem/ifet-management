"""Corrections — superseding a recorded result, through the real route.

Delivery plan TC1g, §6.0 step 2. `corrects_attempt_id` and `correction_reason`
had columns, a property and an envelope mapping since P1 and **nothing set
them**, so every attempt was a retest. There is also no edit or delete route for
an attempt or a shot, by design, so until this route existed a wrongly recorded
result had no route at all.

What is being protected is not our own tidiness. The change document's §0.3
argument to the Airtable team is that a retest and a correction are
indistinguishable without this reference, and that a roll-up counting attempts
would then be wrong **and look right**. These tests are the half of that promise
that lives on our side, so they assert the distinction on the wire and not only
in the database.

Exercised over HTTP rather than the ORM, for the same reason as
`test_manual_tests.py`: the refusals are route rules and are invisible from the
models. Runs against harness Postgres when `M2_DATABASE_URL` is set, SQLite
otherwise — these are application rules, not locking guarantees, so both are
meaningful.
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

# The Airtable record ids the linked fixture carries, matching
# `test_business_acceptance.py` so the two suites describe one world.
REC = {"project": "recPROJ0000000001", "mockup": "recMOCK0000000001",
       "protocol": "recPROT0000000001", "section": "recSECT0000000001"}


def _jpeg(name="evidence.jpg"):
    """A real, decodable JPEG — the uploader downscales before sending."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (180, 40, 40)).save(buf, format="JPEG", quality=80)
    return {"file": (name, io.BytesIO(buf.getvalue()), "image/jpeg")}


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
    # The engine comes back so `tearDown` can dispose it. Every suite here
    # builds one per test and the older ones never let go, which on Postgres
    # means pooled connections held for the whole run — 22 more tests was what
    # finally reached `max_connections`. Disposing is the fix; the same leak in
    # the neighbouring suites is noted rather than refactored from here.
    return TestClient(main.app), Session, engine


@unittest.skipIf(TestClient is None, "fastapi not installed in this environment")
class _Base(unittest.TestCase):
    def setUp(self):
        self.client, self.Session, self._engine = _client_and_session()
        from app.data.models import Device, Project, ProjectParent
        s = self.Session()
        s.add(Device(id=1, name="system-1", turbo_mode=False, turbo_slave=False))
        s.add(ProjectParent(id=1, name="IFET-26-0066"))
        # **Linked to Airtable.** An unlinked job queues nothing by design, so a
        # correction on one would prove the route ran and nothing about whether
        # it publishes — which is the half that matters to the Airtable team.
        s.add(Project(id=1, name="Specimen A", parent_id=1, device_id=1,
                      inward_design_pressure=60.0, outward_design_pressure=45.0,
                      airtable_project_id=REC["project"],
                      airtable_mockup_id=REC["mockup"]))
        s.commit()
        s.close()

    def tearDown(self):
        from app import main
        main.app.dependency_overrides.clear()
        self.client.close()
        self._engine.dispose()

    # -- helpers -----------------------------------------------------------

    def link_test(self, table, test_id):
        """Give a created test its Airtable protocol/section ids."""
        s = self.Session()
        try:
            s.execute(sa.text(
                f"UPDATE {table} SET airtable_protocol_id=:p, "
                "airtable_section_id=:sec, airtable_section_name=:n "
                "WHERE id=:i"),
                {"p": REC["protocol"], "sec": REC["section"],
                 "n": "Forced Entry (*)", "i": test_id})
            s.commit()
        finally:
            s.close()

    def manual_test(self, type_="Forced Entry", option="ASTM F588 Grade 40"):
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": type_, "required_option": option})
        self.assertEqual(r.status_code, 200, r.text)
        self.link_test("manual_tests", r.json()["id"])
        return r.json()

    def impact_test(self):
        r = self.client.post("/projects/1/impact-tests/",
                             json={"missile": "Large Missile D",
                                   "missile_weight": 9.0})
        self.assertEqual(r.status_code, 200, r.text)
        self.link_test("missile_impact_tests", r.json()["id"])
        return r.json()

    def start(self, url, test_id):
        r = self.client.post(f"{url}/{test_id}/trials",
                             json={"operator_name": "technician-1"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def finish(self, attempt_id, **body):
        body.setdefault("testing_continued", "Stopped")
        return self.client.put(f"/test-results/{attempt_id}/finish", json=body)

    def correct(self, attempt_id, **body):
        body.setdefault("reason", "outcome transcribed from the wrong sheet")
        return self.client.post(f"/test-results/{attempt_id}/correct", json=body)

    def recorded(self, attempt_id, **body):
        """A finished Forced Entry attempt, ready to be corrected."""
        test = self.manual_test()
        first = self.start("/projects/1/manual-tests", test["id"])
        self.assertEqual(self.finish(first["id"], result=True).status_code, 200)
        return test, first


class ItSupersedesRatherThanEdits(_Base):
    """The whole point: the original survives and the new row names it."""

    def test_a_correction_names_the_attempt_it_supersedes(self):
        _test, first = self.recorded(None)
        r = self.correct(first["id"], reason="outcome was transcribed wrongly")
        self.assertEqual(r.status_code, 200, r.text)
        correction = r.json()
        self.assertEqual(correction["corrects_attempt_id"],
                         first["labos_attempt_id"])
        self.assertEqual(correction["correction_reason"],
                         "outcome was transcribed wrongly")

    def test_the_original_is_untouched(self):
        """§3: the terminal state is final. A correction must not edit it."""
        _test, first = self.recorded(None)
        before = self.client.get(f"/test-results/{first['id']}").json()
        self.correct(first["id"])
        after = self.client.get(f"/test-results/{first['id']}").json()
        for field in ("status", "test_result", "result", "testing_end_date",
                      "corrects_attempt_id", "trial_number"):
            self.assertEqual(before.get(field), after.get(field),
                             f"{field} changed on the superseded attempt")

    def test_the_correction_groups_with_the_original(self):
        """Same `labos_test_id`, or the two are unrelated rows in Airtable."""
        _test, first = self.recorded(None)
        correction = self.correct(first["id"]).json()
        self.assertEqual(correction["labos_test_id"], first["labos_test_id"])
        self.assertNotEqual(correction["labos_attempt_id"],
                            first["labos_attempt_id"])

    def test_it_takes_the_next_attempt_number(self):
        _test, first = self.recorded(None)
        correction = self.correct(first["id"]).json()
        self.assertEqual(correction["trial_number"], first["trial_number"] + 1)

    def test_the_correction_starts_open_and_pending(self):
        """One lifecycle: it is recorded and finished through the usual paths."""
        _test, first = self.recorded(None)
        correction = self.correct(first["id"]).json()
        self.assertEqual(correction["status"], "In Progress")
        self.assertEqual(correction["test_result"], "Pending")
        self.assertIsNone(correction["testing_end_date"])
        # And it terminates normally.
        self.assertEqual(self.finish(correction["id"], result=False).status_code,
                         200)

    def test_the_corrector_need_not_be_the_original_operator(self):
        _test, first = self.recorded(None)
        correction = self.correct(first["id"], operator_name="supervisor-2").json()
        self.assertEqual(correction["operator_name"], "supervisor-2")
        self.assertEqual(first["operator_name"], "technician-1")

    def test_it_defaults_to_the_original_operator(self):
        _test, first = self.recorded(None)
        correction = self.correct(first["id"]).json()
        self.assertEqual(correction["operator_name"], first["operator_name"])


class ARetestIsNotACorrection(_Base):
    """The distinction the Airtable team was promised, asserted on the wire."""

    def test_an_ordinary_retest_carries_no_reference(self):
        test, first = self.recorded(None)
        retest = self.start("/projects/1/manual-tests", test["id"])
        self.assertIsNone(retest["corrects_attempt_id"])
        self.assertIsNone(retest["correction_reason"])

    def test_a_correction_is_distinguishable_from_a_retest(self):
        """Both are new attempts sharing a test id; only one names a predecessor."""
        test, first = self.recorded(None)
        correction = self.correct(first["id"]).json()
        self.assertEqual(self.finish(correction["id"], result=False).status_code,
                         200)
        retest = self.start("/projects/1/manual-tests", test["id"])

        self.assertEqual(correction["labos_test_id"], retest["labos_test_id"])
        self.assertIsNotNone(correction["corrects_attempt_id"])
        self.assertIsNone(retest["corrects_attempt_id"])

    def test_is_correction_reflects_the_reference_not_the_ordinal(self):
        """`trial_number > 1` never implies a correction."""
        from app.data.models import TestResult
        test, first = self.recorded(None)
        correction = self.correct(first["id"]).json()
        s = self.Session()
        try:
            rows = {r.id: r for r in s.query(TestResult).all()}
            self.assertTrue(rows[correction["id"]].is_correction)
            self.assertFalse(rows[first["id"]].is_correction)
        finally:
            s.close()


class ItRefusesWhatItShould(_Base):
    def test_an_open_attempt_cannot_be_corrected(self):
        """Finish it correctly instead — nothing is frozen yet."""
        test = self.manual_test()
        first = self.start("/projects/1/manual-tests", test["id"])
        r = self.correct(first["id"])
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("not finished", r.text)

    def test_a_correction_requires_a_reason(self):
        """Contract §4.1 puts it beside the reference in the required set."""
        _test, first = self.recorded(None)
        for reason in ("", "   "):
            r = self.client.post(f"/test-results/{first['id']}/correct",
                                 json={"reason": reason})
            self.assertEqual(r.status_code, 400, r.text)

    def test_an_absent_reason_is_a_validation_error(self):
        _test, first = self.recorded(None)
        r = self.client.post(f"/test-results/{first['id']}/correct", json={})
        self.assertEqual(r.status_code, 422, r.text)

    def test_an_unknown_attempt_is_not_found(self):
        r = self.correct(99999)
        self.assertEqual(r.status_code, 404)

    def test_it_refuses_while_another_attempt_is_open(self):
        """One test, one open attempt — otherwise "current" is ambiguous."""
        test, first = self.recorded(None)
        self.start("/projects/1/manual-tests", test["id"])       # a retest, open
        r = self.correct(first["id"])
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("still open", r.text)

    def test_an_aborted_attempt_can_be_corrected(self):
        """Abort is terminal, and an abort recorded in error is still an error."""
        test = self.manual_test()
        first = self.start("/projects/1/manual-tests", test["id"])
        self.assertEqual(
            self.finish(first["id"], abort_reason="power loss").status_code, 200)
        r = self.correct(first["id"], reason="aborted the wrong specimen")
        self.assertEqual(r.status_code, 200, r.text)


class ItWorksOnAFinishedTest(_Base):
    """A finished test is where a correction is most likely to be needed."""

    def test_a_finished_test_still_accepts_a_correction(self):
        test, first = self.recorded(None)
        done = self.client.put(f"/projects/1/manual-tests/{test['id']}/finish")
        self.assertEqual(done.status_code, 200, done.text)

        r = self.correct(first["id"], reason="report already issued, grade wrong")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["corrects_attempt_id"],
                         first["labos_attempt_id"])

    def test_a_finished_test_still_refuses_a_retest(self):
        """The contrast that makes the test above meaningful, not incidental."""
        test, _first = self.recorded(None)
        self.client.put(f"/projects/1/manual-tests/{test['id']}/finish")
        r = self.client.post(f"/projects/1/manual-tests/{test['id']}/trials",
                             json={"operator_name": "technician-1"})
        self.assertEqual(r.status_code, 400, r.text)


class ItCoversEveryTestType(_Base):
    def test_impact_attempts_can_be_corrected(self):
        test = self.impact_test()
        first = self.start("/projects/1/impact-tests", test["id"])
        shot = self.client.post(f"/test-results/{first['id']}/shots",
                                json={"result": True})
        self.assertEqual(shot.status_code, 200, shot.text)
        # §4.5 rule 3: Impact requires photographic evidence at finish. Not a
        # detail of this test — an impact attempt cannot terminate without it.
        photo = self.client.post(f"/shots/{shot.json()['id']}/photos",
                                 files=_jpeg())
        self.assertEqual(photo.status_code, 200, photo.text)
        self.assertEqual(self.finish(first["id"], result=True).status_code, 200)

        r = self.correct(first["id"], reason="impact recorded as a pass in error")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["test_type"], "Impact")

    def test_ansi_attempts_can_be_corrected(self):
        test = self.manual_test(type_="ANSI Z97.1", option="Class A")
        first = self.start("/projects/1/manual-tests", test["id"])
        self.assertEqual(self.finish(first["id"], result=True).status_code, 200)
        r = self.correct(first["id"], reason="class judged against was wrong")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["test_type"], "ANSI Z97.1")


class ItReachesTheOutbox(_Base):
    """A correction must be published, and as a create rather than an edit."""

    def test_a_correction_is_queued_with_its_reference(self):
        from app.sync.outbox import SyncOutbox
        _test, first = self.recorded(None)
        correction = self.correct(first["id"],
                                  reason="transcription error on the sheet").json()

        s = self.Session()
        try:
            rows = (s.query(SyncOutbox)
                    .filter(SyncOutbox.attempt_id == correction["labos_attempt_id"])
                    .all())
            self.assertTrue(rows, "the correction queued nothing")
            self.assertIn("create", {r.phase for r in rows})
            payload = next(r.payload for r in rows if r.phase == "create")
            fields = payload.get("fields", payload)
            self.assertEqual(fields.get("Corrects Attempt ID"),
                             first["labos_attempt_id"])
            self.assertEqual(fields.get("Correction Reason"),
                             "transcription error on the sheet")
        finally:
            s.close()

    def test_the_original_is_not_requeued(self):
        """Superseding is not editing: the original's rows must not change."""
        from app.sync.outbox import SyncOutbox
        _test, first = self.recorded(None)
        s = self.Session()
        try:
            before = (s.query(SyncOutbox)
                      .filter(SyncOutbox.attempt_id == first["labos_attempt_id"])
                      .count())
        finally:
            s.close()

        self.correct(first["id"])

        s = self.Session()
        try:
            after = (s.query(SyncOutbox)
                     .filter(SyncOutbox.attempt_id == first["labos_attempt_id"])
                     .count())
        finally:
            s.close()
        self.assertEqual(before, after)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
