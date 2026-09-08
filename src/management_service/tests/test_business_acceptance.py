"""Business acceptance — the whole round trip, per test type, through HTTP.

Delivery plan §4.7 and §6.1. What this file proves, and why each piece is here:

*   **All five test types**, because until 2026-09-08 the outbound mapper
    resolved two of them and an acceptance run over Static and Cycles alone
    would have passed while Impact, Forced Entry and ANSI could not be published
    at all.
*   **Retesting**, because the Airtable team's model turns on a second attempt
    sharing one `LabOS Test ID` while carrying a new `LabOS Attempt ID` and the
    next `Attempt Number`.
*   **Outage and restart recovery**, because the reason the outbox exists is that
    an Airtable problem must never reach the operator.
*   **The two channels**, because an attachment sharing the record FIFO meant a
    parked photograph could block a verdict.
*   **A job with no Airtable origin**, because that is the normal standalone mode
    (§4.6) and it must queue nothing rather than fail.

The unmet requirements are asserted as **expected absences** rather than left
out of the tests: `Max Pressure Achieved`, `Deflection Value` and
`Deflection Unit` must not appear in any payload. Narrowing the tests until they
pass would hide exactly the thing the reconciliation set out to make visible.

Runs on harness Postgres when `M2_DATABASE_URL` is set. Ordering, the channel
split and the constraint are all meaningful on both backends; concurrent
allocation is Postgres-only and marked where it matters.
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

# Fields the contract withholds. None may ever reach a payload (§4.4, A2/A3).
WITHHELD = ("Max Pressure Achieved", "Deflection Value", "Deflection Unit")

def _now():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc)


REC = {"project": "recPROJ0000000001", "mockup": "recMOCK0000000001",
       "protocol": "recPROT0000000001", "section": "recSECT0000000001"}


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
    """One Airtable-linked project, and one that has no Airtable origin."""

    def setUp(self):
        self.client, self.Session = _client_and_session()
        from app.data.models import Device, Project, ProjectParent
        s = self.Session()
        s.add(Device(id=1, name="system-1", turbo_mode=False, turbo_slave=False))
        s.add(ProjectParent(id=1, name="IFET-26-0066"))
        # Linked: imported from Airtable.
        s.add(Project(id=1, name="Specimen A", parent_id=1, device_id=1,
                      inward_design_pressure=60.0, outward_design_pressure=45.0,
                      airtable_project_id=REC["project"],
                      airtable_mockup_id=REC["mockup"]))
        # Standalone: an operator typed it. No rec… ids anywhere.
        s.add(Project(id=2, name="Specimen B", parent_id=1, device_id=1,
                      inward_design_pressure=60.0, outward_design_pressure=45.0))
        s.commit()
        s.close()

    def tearDown(self):
        from app import main
        main.app.dependency_overrides.clear()

    # -- helpers ----------------------------------------------------------
    def queue(self, attempt_id=None, channel=None):
        """Outbox entries, ordered as the worker would take them."""
        from app.sync.outbox import SyncOutbox
        s = self.Session()
        try:
            q = s.query(SyncOutbox)
            if attempt_id:
                q = q.filter(SyncOutbox.attempt_id == attempt_id)
            rows = q.order_by(SyncOutbox.attempt_id, SyncOutbox.attempt_seq).all()
            out = [(r.phase, r.attempt_seq, r.state, dict(r.payload or {}))
                   for r in rows]
        finally:
            s.close()
        if channel == "record":
            out = [r for r in out if r[0] != "attachment"]
        elif channel == "attachment":
            out = [r for r in out if r[0] == "attachment"]
        return out

    def link_test(self, table, test_id):
        """Give a created test its Airtable protocol/section ids."""
        s = self.Session()
        try:
            s.execute(sa.text(
                f"UPDATE {table} SET airtable_protocol_id=:p, "
                "airtable_section_id=:sec, airtable_section_name=:n "
                "WHERE id=:i"),
                {"p": REC["protocol"], "sec": REC["section"],
                 "n": "DP (+) (PSF)", "i": test_id})
            s.commit()
        finally:
            s.close()

    def assert_no_withheld(self, payload):
        for field in WITHHELD:
            self.assertNotIn(
                field, payload,
                f"{field} must never be published — it has no validated source "
                "(contract §4.4). Its absence is the decision, not an oversight.")


class ManualAndImpactRoundTrip(_Base):
    """Forced Entry, ANSI Z97.1 and Impact — the three built for the UI."""

    def _create(self, kind, **body):
        if kind == "impact":
            r = self.client.post("/projects/1/impact-tests/", json=body or {})
            table = "missile_impact_tests"
        else:
            r = self.client.post("/projects/1/manual-tests/", json=body)
            table = "manual_tests"
        self.assertEqual(r.status_code, 200, r.text)
        test_id = r.json()["id"]
        self.link_test(table, test_id)
        return test_id

    def _start(self, kind, test_id):
        path = ("impact-tests" if kind == "impact" else "manual-tests")
        r = self.client.post(f"/projects/1/{path}/{test_id}/trials",
                             json={"operator_name": "technician-1"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_forced_entry_all_four_phases_in_order(self):
        test_id = self._create("manual", type="Forced Entry",
                               required_option="ASTM F588 Grade 40")
        attempt = self._start("manual", test_id)
        aid, pk = attempt["labos_attempt_id"], attempt["id"]

        # create — the phase that never fired for a manual test before today
        entries = self.queue(aid)
        self.assertEqual([e[0] for e in entries], ["create"])
        payload = entries[0][3]
        self.assertEqual(payload["LabOS Attempt ID"], aid)
        self.assertEqual(payload["Test Result"], "Pending")
        self.assertEqual(payload["Airtable Project ID"], REC["project"])
        self.assertEqual(payload["Airtable Section ID"], REC["section"])
        self.assert_no_withheld(payload)

        # evidence, then terminal, then verdict
        self.assertEqual(self.client.post(f"/test-results/{pk}/photos",
                                          files=_jpeg()).status_code, 200)
        self.assertEqual(self.client.put(
            f"/test-results/{pk}/finish",
            json={"result": True, "testing_continued": "Stopped"}).status_code, 200)
        self.assertEqual(self.client.put(
            f"/test-results/{pk}/verdict",
            json={"test_result": "Pass", "verdict_by": "reviewer-1",
                  "retest_required": False}).status_code, 200)

        record = [e[0] for e in self.queue(aid, channel="record")]
        self.assertEqual(record, ["create", "terminal", "verdict"],
                         "the record channel must stay in phase order")
        attach = self.queue(aid, channel="attachment")
        self.assertEqual(len(attach), 1, "one entry per photograph")

        verdict = [e[3] for e in self.queue(aid, channel="record")][-1]
        self.assertEqual(verdict["Test Result"], "Passed",
                         "the wire spelling is theirs, not ours")
        self.assertEqual(verdict["LabOS Verdict By"], "reviewer-1")
        self.assert_no_withheld(verdict)

    def test_ansi_publishes_a_class_and_no_number(self):
        test_id = self._create("manual", type="ANSI Z97.1", required_option="Class A")
        attempt = self._start("manual", test_id)
        payload = self.queue(attempt["labos_attempt_id"])[0][3]
        self.assertEqual(payload["Test Type"], "ANSI Z97.1")
        self.assert_no_withheld(payload)

    def test_impact_publishes_and_keeps_per_impact_detail_local(self):
        test_id = self._create("impact")
        attempt = self._start("impact", test_id)
        aid, pk = attempt["labos_attempt_id"], attempt["id"]

        shot = self.client.post(f"/test-results/{pk}/shots", json={"result": True})
        self.assertEqual(shot.status_code, 200, shot.text)
        sid = shot.json()["id"]
        self.assertEqual(self.client.post(f"/shots/{sid}/photos",
                                          files=_jpeg()).status_code, 200)
        self.assertEqual(self.client.put(
            f"/test-results/{pk}/finish",
            json={"result": True, "testing_continued": "Stopped"}).status_code, 200)

        self.assertEqual([e[0] for e in self.queue(aid, channel="record")],
                         ["create", "terminal"])
        attach = self.queue(aid, channel="attachment")
        self.assertEqual(len(attach), 1)
        self.assertEqual(attach[0][3]["photo"]["shot_id"], sid,
                         "a per-impact photograph must say which impact it is of")

    def test_a_photograph_added_between_finish_and_verdict_is_queued(self):
        """The freeze point is the verdict, not termination (contract §6).

        The first draft of §4.7 snapshotted the photo set at termination, which
        would have silently dropped this one — `_save_photo` permits it, and
        only the verdict makes it a 409.
        """
        test_id = self._create("manual", type="Forced Entry", required_option="Grade 40")
        attempt = self._start("manual", test_id)
        aid, pk = attempt["labos_attempt_id"], attempt["id"]
        self.client.put(f"/test-results/{pk}/finish",
                        json={"result": True, "testing_continued": "Stopped"})

        after = self.client.post(f"/test-results/{pk}/photos", files=_jpeg("late.jpg"))
        self.assertEqual(after.status_code, 200,
                         "evidence is addable until review")
        self.assertEqual(len(self.queue(aid, channel="attachment")), 1)

        self.client.put(f"/test-results/{pk}/verdict",
                        json={"test_result": "Pass", "verdict_by": "reviewer-1",
                              "retest_required": False})
        frozen = self.client.post(f"/test-results/{pk}/photos", files=_jpeg("later.jpg"))
        self.assertEqual(frozen.status_code, 409, "and frozen after it")
        self.assertEqual(len(self.queue(aid, channel="attachment")), 1,
                         "a refused upload queues nothing")

    def test_retest_shares_the_test_id_and_increments_the_number(self):
        """The Airtable team's retest model, asserted end to end."""
        test_id = self._create("manual", type="Forced Entry", required_option="Grade 40")
        first = self._start("manual", test_id)
        self.client.put(f"/test-results/{first['id']}/finish",
                        json={"result": False, "testing_continued": "Stopped"})
        second = self._start("manual", test_id)

        self.assertEqual(first["labos_test_id"], second["labos_test_id"],
                         "attempts at one test share one LabOS Test ID")
        self.assertNotEqual(first["labos_attempt_id"], second["labos_attempt_id"])
        self.assertEqual([first["trial_number"], second["trial_number"]], [1, 2])

        p1 = self.queue(first["labos_attempt_id"])[0][3]
        p2 = self.queue(second["labos_attempt_id"])[0][3]
        for field in ("Airtable Project ID", "Airtable Mockup ID",   # the wire name has no hyphen
                      "Airtable Protocol ID", "Airtable Section ID",
                      "LabOS Test ID"):
            self.assertEqual(p1[field], p2[field],
                             f"{field} must not change between attempts")
        self.assertIsNone(p2.get("Corrects Attempt ID"),
                          "a retest is not a correction")

    def test_labos_test_id_is_one_format_across_all_five_types(self):
        """One column, one vocabulary — DG12.

        A UUID, not a slug: the change document tells the Airtable team to group
        on equality and never parse, and a readable slug both invited parsing
        and leaked a database primary key.
        """
        import uuid
        ids = []
        for kind, body in (("manual", {"type": "Forced Entry", "required_option": "g"}),
                           ("manual", {"type": "ANSI Z97.1", "required_option": "A"}),
                           ("impact", {})):
            tid = self._create(kind, **body)
            attempt = self._start(kind, tid)
            ids.append(attempt["labos_test_id"])
            # One rig, one run: terminate before starting the next test.
            self.client.put(f"/test-results/{attempt['id']}/finish",
                            json={"abort_reason": "Equipment Fault"})
        for value in ids:
            uuid.UUID(value)   # raises unless it is a real UUID
            self.assertNotIn("-test-", value)
            self.assertFalse(value.startswith(("impact-", "forced-entry-", "ansi")))

    def test_a_job_with_no_airtable_origin_queues_nothing(self):
        """Standalone is the normal mode, not a degraded one (§4.6)."""
        r = self.client.post("/projects/2/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "Grade 40"})
        self.assertEqual(r.status_code, 200, r.text)
        attempt = self.client.post(
            f"/projects/2/manual-tests/{r.json()['id']}/trials",
            json={"operator_name": "technician-1"})
        self.assertEqual(attempt.status_code, 200, attempt.text)
        aid = attempt.json()["labos_attempt_id"]
        self.assertEqual(self.queue(aid), [],
                         "a local job must never be queued for Airtable")

        from app.data.models import TestResult
        s = self.Session()
        try:
            row = s.query(TestResult).filter(
                TestResult.labos_attempt_id == aid).one()
            self.assertEqual(row.airtable_sync_state, "Excluded")
        finally:
            s.close()


class RigTypeRoundTrip(_Base):
    """Static Load and Cycles — the two that post a finished trial in one call."""

    def _static_test(self):
        s = self.Session()
        try:
            s.execute(sa.text(
                "INSERT INTO static_tests (id, finished, index, pressure_factor, "
                "pressure, duration, type, preset, project_id, "
                "airtable_protocol_id, airtable_section_id, airtable_section_name) "
                "VALUES (1, false, 0, '0.75', 45.0, 10, 'static', false, 1, "
                ":p, :sec, 'DP (+) (PSF)')"),
                {"p": REC["protocol"], "sec": REC["section"]})
            s.commit()
        finally:
            s.close()

    def test_static_trial_queues_a_create_without_the_withheld_fields(self):
        self._static_test()
        r = self.client.post("/projects/1/static_tests/0/trials", json={
            "result": True, "deflections": [
                {"deflection_gauge": "g1", "max_deflection": 1234.0,
                 "permanent_deflection": 12.0, "recovery": 60.0}]})
        self.assertIn(r.status_code, (200, 201), r.text)

        entries = self.queue()
        self.assertTrue(entries, "a linked static trial must queue a phase")
        payload = entries[0][3]
        self.assert_no_withheld(payload)
        self.assertNotIn("Measured Value", payload,
                         "A2 — the rig sends no measurement for static load")


class ChannelsAndRecovery(_Base):
    """The queue's own guarantees, at the level the worker sees them."""

    def _linked_attempt(self):
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "Grade 40"})
        test_id = r.json()["id"]
        self.link_test("manual_tests", test_id)
        a = self.client.post(f"/projects/1/manual-tests/{test_id}/trials",
                             json={"operator_name": "technician-1"}).json()
        return a["labos_attempt_id"], a["id"]

    def test_a_parked_attachment_does_not_block_the_verdict(self):
        """Finding #8 — the reason attachments needed their own channel.

        Heads were grouped by attempt alone, so an attachment queued before the
        verdict became the head, and `claim()`'s own docstring says a parked
        head makes the whole attempt undeliverable. A photograph that will never
        upload would have held a measured verdict hostage.
        """
        from app.sync import outbox
        aid, pk = self._linked_attempt()
        self.client.post(f"/test-results/{pk}/photos", files=_jpeg())
        self.client.put(f"/test-results/{pk}/finish",
                        json={"result": True, "testing_continued": "Stopped"})
        self.client.put(f"/test-results/{pk}/verdict",
                        json={"test_result": "Pass", "verdict_by": "reviewer-1",
                              "retest_required": False})

        s = self.Session()
        try:
            attach = [e for e in s.query(outbox.SyncOutbox)
                      .filter(outbox.SyncOutbox.attempt_id == aid).all()
                      if e.phase == "attachment"]
            self.assertEqual(len(attach), 1)
            attach[0].state = outbox.PARKED
            s.commit()

            phases = [e.phase for e in outbox.claim(s, limit=10)]
        finally:
            s.close()
        self.assertIn("create", phases,
                      "the record channel must still be claimable with a "
                      "parked attachment in the attempt")
        self.assertNotIn("attachment", phases, "a parked entry is not claimable")

    def test_record_phases_stay_strictly_ordered(self):
        from app.sync import outbox
        aid, pk = self._linked_attempt()
        self.client.put(f"/test-results/{pk}/finish",
                        json={"result": True, "testing_continued": "Stopped"})
        self.client.put(f"/test-results/{pk}/verdict",
                        json={"test_result": "Pass", "verdict_by": "reviewer-1",
                              "retest_required": False})
        s = self.Session()
        try:
            claimed = outbox.claim(s, limit=10)
            self.assertEqual([e.phase for e in claimed], ["create"],
                             "terminal must wait for create to land")
        finally:
            s.close()

    def test_an_outage_never_reaches_the_operator(self):
        """The whole reason the outbox exists.

        Nothing in the request path can call Airtable — enforced by
        `test_report_api_isolation` — so the strongest statement here is the
        behavioural one: every route still returns 200 and the work is queued,
        with no Airtable configured at all.
        """
        from app.config import airtable_settings
        self.assertFalse(
            airtable_settings.sync_enabled,
            "the test environment must have sync off; the save must not care")
        aid, pk = self._linked_attempt()
        for call in (
            lambda: self.client.post(f"/test-results/{pk}/photos", files=_jpeg()),
            lambda: self.client.put(f"/test-results/{pk}/finish",
                                    json={"result": True,
                                          "testing_continued": "Stopped"}),
            lambda: self.client.put(f"/test-results/{pk}/verdict",
                                    json={"test_result": "Pass",
                                          "verdict_by": "reviewer-1",
                                          # Required by design: an unchecked box
                                          # is not a decision (VerdictSchema).
                                          "retest_required": False}),
        ):
            self.assertEqual(call().status_code, 200)
        self.assertEqual(len(self.queue(aid)), 4,
                         "create, terminal, verdict and one attachment, all "
                         "waiting for a worker that is not running")

    def test_a_restart_resumes_from_the_queue_not_from_memory(self):
        """Recovery is a property of the table, so a new session must see it."""
        from app.sync import outbox
        aid, pk = self._linked_attempt()
        self.client.put(f"/test-results/{pk}/finish",
                        json={"result": True, "testing_continued": "Stopped"})

        # A worker takes the head, then dies without reporting.
        s1 = self.Session()
        try:
            first = outbox.claim(s1, limit=1)[0]
            self.assertEqual(first.phase, "create")
            s1.commit()
        finally:
            s1.close()

        # A brand-new session — the restart — must find the lease and, once it
        # has expired, take the same entry again rather than skipping it.
        import datetime as dt
        s2 = self.Session()
        try:
            again = outbox.claim(
                s2, limit=1,
                now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
            self.assertEqual([e.phase for e in again], ["create"],
                            "an expired lease must be re-claimable after a crash")
        finally:
            s2.close()


class UnmetRequirementsStayVisible(_Base):
    """The six UNMET rows, asserted as absences rather than omitted from tests."""

    def test_no_payload_may_carry_a_withheld_measurement(self):
        from app.airtable import envelope
        from app.airtable.envelope import EnvelopeError
        base = {"LabOS Attempt ID": "a", "LabOS Test ID": "t",
                "Attempt Number": 1, "Test Type": "Static Load",
                "Operator Name": "technician-1"}
        for field, value in (("Max Pressure Achieved", 61.0),
                             ("Deflection Value", 1234.0),
                             ("Deflection Unit", "in")):
            with self.assertRaises(EnvelopeError, msg=f"{field} was accepted"):
                envelope.build_start({**base, field: value})

    def test_recovery_is_a_config_constant_and_is_never_published(self):
        """Firmware audit 2026-08-31 — a number that is not a measurement.

        The most dangerous of the six, because a value *is* present: `recovery`
        is the rig's `recovery_time` config constant, so publishing it would
        report a setting as an observation.
        """
        from app.airtable.mapping import envelope_values
        from app.data.models import Deflection, ManualTest, ManualTestResult
        s = self.Session()
        try:
            s.add(ManualTest(id=1, project_id=1, type="Forced Entry",
                             required_option="Grade 40", finished=False,
                             airtable_protocol_id=REC["protocol"],
                             airtable_section_id=REC["section"]))
            s.flush()
            attempt = ManualTestResult(manual_test_id=1, trial_number=1,
                                       labos_attempt_id="a1", labos_test_id="t1",
                                       test_type="Forced Entry", status="Completed")
            s.add(attempt)
            s.flush()
            s.add(Deflection(deflection_gauge="g1", max_deflection=1234.0,
                             permanent_deflection=12.0, recovery=60.0,
                             test_id=attempt.id))
            s.commit()
            values = envelope_values(attempt, strict=True)
        finally:
            s.close()
        self.assertNotIn("recovery", values)
        self.assertNotIn("Recovery", values)
        for field in WITHHELD:
            self.assertIsNone(values.get(field),
                              f"{field} must be absent or None, never a number")


class CyclicRoundTrip(_Base):
    """Cycles — the fifth type, so an acceptance run cannot pass on four."""

    def test_cyclic_trial_queues_a_create_and_withholds_the_measurements(self):
        s = self.Session()
        try:
            s.execute(sa.text(
                "INSERT INTO cyclic_tests (id, finished, index, type, cycles, "
                "low_pressure, high_pressure, resume, current_cycle, preset, "
                "project_id, airtable_protocol_id, airtable_section_id, "
                "airtable_section_name) VALUES (1, false, 0, 'cyclic', 1000, "
                "30.0, 60.0, false, 0, false, 1, :p, :sec, 'DP (+) (PSF)')"),
                {"p": REC["protocol"], "sec": REC["section"]})
            s.commit()
        finally:
            s.close()
        r = self.client.post("/projects/1/cyclic-tests/0/trials", json={
            "result": True, "deflections": [
                {"deflection_gauge": "g1", "max_deflection": 980.0,
                 "permanent_deflection": 8.0, "recovery": 60.0}]})
        self.assertIn(r.status_code, (200, 201), r.text)

        entries = self.queue()
        self.assertTrue(entries, "a linked cyclic trial must queue a phase")
        payload = entries[0][3]
        self.assert_no_withheld(payload)
        self.assertEqual(payload["Test Type"], "Cycles")

        # The withheld measurements must be *explained* in the JSON, not merely
        # absent — that is what tells a consumer "not trusted" from "not taken".
        import json as _json
        detail = _json.loads(payload["Complete LabOS JSON Response"])
        reasons = {r["field"]: r["reason"] for r in detail.get("data_quality", [])}
        self.assertEqual(reasons.get("Deflection Value"), "uncalibrated_gauge_counts")
        self.assertEqual(reasons.get("recovery"), "not_a_measurement")
        self.assertEqual(reasons.get("Max Pressure Achieved"), "not_persisted")
        self.assertEqual(reasons.get("Measured Value"), "no_source")
        self.assertNotIn("deflections", detail,
                         "the untrusted numbers stay in LabOS (contract §5)")


class DeliveryThroughTheWorker(_Base):
    """The other half of the round trip: the queue actually drains, in order.

    Driven with a synthetic transport, which is how `sync.worker` is built to be
    tested — `run_cycle(session, send)` takes the sender. This is also the
    correction to §4.7's first test plan, which proposed running the real worker
    with a blank token: `service.py` exits 0 when Airtable is not configured, so
    that would have started nothing and proved nothing.
    """

    def _completed_attempt(self):
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry",
                                   "required_option": "ASTM F588 Grade 40"})
        test_id = r.json()["id"]
        self.link_test("manual_tests", test_id)
        a = self.client.post(f"/projects/1/manual-tests/{test_id}/trials",
                             json={"operator_name": "technician-1"}).json()
        self.client.post(f"/test-results/{a['id']}/photos", files=_jpeg())
        self.client.put(f"/test-results/{a['id']}/finish",
                        json={"result": True, "testing_continued": "Stopped"})
        self.client.put(f"/test-results/{a['id']}/verdict",
                        json={"test_result": "Pass", "verdict_by": "reviewer-1",
                              "retest_required": False})
        return a["labos_attempt_id"], a["id"]

    def test_every_phase_delivers_and_the_queue_empties(self):
        from app.sync import worker
        aid, _ = self._completed_attempt()
        sent = []

        def send(entry):
            # `send(entry) -> record_id | None`, per worker.run_cycle. Returning
            # the whole API response instead put a dict where a string goes.
            sent.append((entry.phase, entry.attempt_seq))
            return "recDELIVERED00001"

        s = self.Session()
        try:
            worker.drain(s, send)
        finally:
            s.close()

        record = [p for p, _ in sent if p != "attachment"]
        self.assertEqual(record, ["create", "terminal", "verdict"],
                         "phases must reach Airtable in order")
        self.assertEqual(sum(1 for p, _ in sent if p == "attachment"), 1)
        self.assertEqual([e for e in self.queue(aid) if e[2] != "done"], [],
                         "a fully delivered attempt leaves nothing open")

    def test_an_outage_mid_drain_leaves_the_rest_queued_and_recovers(self):
        """The failure the outbox exists for, and the recovery after it."""
        from app.airtable.errors import AirtableServerError
        from app.sync import worker
        aid, _ = self._completed_attempt()
        attempts_seen = []

        def failing(entry):
            attempts_seen.append(entry.phase)
            raise AirtableServerError("Airtable returned 503")

        s = self.Session()
        try:
            worker.run_cycle(s, failing)
            open_after = [e for e in self.queue(aid) if e[2] != "done"]
            self.assertTrue(open_after,
                            "a 503 must leave the work queued, not drop it")

            # Recovery: the same entries deliver once the service returns —
            # but only after the backoff the failure scheduled, so the clock has
            # to move. Asserting recovery "immediately" would be asserting that
            # the backoff does not work.
            import datetime as _dt
            worker.drain(s, lambda e: "recOK",
                         now=_dt.datetime.now(_dt.timezone.utc)
                             + _dt.timedelta(hours=1))
        finally:
            s.close()
        self.assertEqual([e for e in self.queue(aid) if e[2] != "done"], [],
                         "everything queued during the outage must eventually land")

    def test_status_reports_the_queue_in_the_four_contractual_words(self):
        aid, _ = self._completed_attempt()
        body = self.client.get("/sync/status").json()
        self.assertIn(body["status"],
                      ("Synced", "Pending", "Sync Failed", "Retry Required"))
        self.assertEqual(body["status"], "Sync Failed",
                         "no worker has ever beaten, so liveness is the failure")
        self.assertGreaterEqual(body["queue_depth"], 3)
        self.assertGreaterEqual(body["attachment_backlog"], 1)

        queue = self.client.get("/sync/queue").json()
        phases = [e["phase"] for e in queue["entries"]]
        self.assertEqual([p for p in phases if p != "attachment"],
                         ["create", "terminal", "verdict"])
        channels = {e["phase"]: e["channel"] for e in queue["entries"]}
        self.assertEqual(channels["attachment"], "attachment")
        self.assertEqual(channels["verdict"], "record")

    def test_retry_only_accepts_a_parked_entry(self):
        from app.sync import outbox
        aid, _ = self._completed_attempt()
        s = self.Session()
        try:
            entry = (s.query(outbox.SyncOutbox)
                     .filter(outbox.SyncOutbox.attempt_id == aid)
                     .order_by(outbox.SyncOutbox.attempt_seq).first())
            entry_id, = entry.id,
            self.assertEqual(
                self.client.post(f"/sync/queue/{entry_id}/retry").status_code, 400,
                "a pending entry needs no retry; the worker has it")
            entry.state = outbox.PARKED
            s.commit()
        finally:
            s.close()
        r = self.client.post(f"/sync/queue/{entry_id}/retry")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "pending")
        self.assertEqual(self.client.post("/sync/queue/999999/retry").status_code, 404)


class ConcurrentStarts(_Base):
    """Two starts at once must produce two attempts, not one number twice.

    Postgres only, and that is the point: the constraint and the savepoint retry
    are both invisible on SQLite, where a single writer means there is no race
    to lose.
    """

    def setUp(self):
        if not os.environ.get("M2_DATABASE_URL", "").startswith("postgresql"):
            self.skipTest("needs the Postgres harness — no race exists on SQLite")
        super().setUp()

    def test_a_duplicate_attempt_number_is_impossible(self):
        from sqlalchemy.exc import IntegrityError
        from app.data.models import ManualTest, ManualTestResult
        s = self.Session()
        try:
            s.add(ManualTest(id=1, project_id=1, type="Forced Entry",
                             required_option="Grade 40", finished=False))
            s.commit()
            common = dict(manual_test_id=1, test_type="Forced Entry",
                          status="In Progress", labos_test_id="shared-test-id")
            s.add(ManualTestResult(trial_number=1, labos_attempt_id="a1", **common))
            s.commit()
            s.add(ManualTestResult(trial_number=1, labos_attempt_id="a2", **common))
            with self.assertRaises(IntegrityError,
                                   msg="two attempt 1s of one test were accepted"):
                s.commit()
        finally:
            s.rollback()
            s.close()

    def test_sequential_starts_number_in_order(self):
        """The ordinary case. **Not a concurrency test** — see the next one.

        Three sequential HTTP calls exercise `next_attempt_number` and nothing
        else: each sees the previous commit, so no collision ever occurs and the
        retry loop is never entered. This was labelled a concurrency test until
        2026-09-08, which is how a retry loop that crashed on its first
        collision sat behind a passing suite.
        """
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "g"})
        test_id = r.json()["id"]
        numbers = []
        for _ in range(3):
            a = self.client.post(f"/projects/1/manual-tests/{test_id}/trials",
                                 json={"operator_name": "technician-1"})
            self.assertEqual(a.status_code, 200, a.text)
            numbers.append(a.json()["trial_number"])
            # Terminate between starts: a Start while one is open returns the
            # open attempt, which is the point of `test_a_duplicate_start_*`.
            self.client.put(f"/test-results/{a.json()['id']}/finish",
                            json={"result": False,
                                  "testing_continued": "Stopped"})
        self.assertEqual(numbers, [1, 2, 3])

    def test_a_duplicate_start_returns_the_open_attempt(self):
        """One test runs once at a time, so Start is idempotent.

        A double-clicked Start must not become two certification records for one
        physical test. Until 2026-09-08 it allocated the next number, and
        Airtable would have shown attempts 1 and 2 for a single run.
        """
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "g"})
        test_id = r.json()["id"]
        first = self.client.post(f"/projects/1/manual-tests/{test_id}/trials",
                                 json={"operator_name": "technician-1"}).json()
        again = self.client.post(f"/projects/1/manual-tests/{test_id}/trials",
                                 json={"operator_name": "technician-1"}).json()
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["trial_number"], 1)
        self.assertEqual(again["labos_attempt_id"], first["labos_attempt_id"])

    def test_one_active_run_per_rig(self):
        """A second test on a busy rig cannot correspond to anything physical."""
        a = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "g"})
        b = self.client.post("/projects/1/manual-tests/",
                             json={"type": "ANSI Z97.1", "required_option": "A"})
        self.client.post(f"/projects/1/manual-tests/{a.json()['id']}/trials",
                         json={"operator_name": "technician-1"})
        busy = self.client.post(
            f"/projects/1/manual-tests/{b.json()['id']}/trials",
            json={"operator_name": "technician-1"})
        self.assertEqual(busy.status_code, 409, busy.text)
        self.assertIn("one test at a time", busy.json()["detail"])

    def test_a_real_collision_is_recovered_not_raised(self):
        """Two starts that genuinely race, on separate connections.

        A barrier makes both read `max(trial_number)` before either inserts, so
        one *must* lose the unique constraint. That is the only way to reach
        `insert_attempt`'s retry loop, and when it was first reached the loop
        raised `InvalidRequestError` instead of retrying: exiting
        `begin_nested()` on an exception already discards the pending object, so
        the unconditional `session.expunge(obj)` failed.

        Two threads and two connections, because a single session cannot race
        itself and SQLAlchemy would serialise it.
        """
        import threading
        from app.data.attempts import insert_attempt, next_attempt_number
        from app.data.models import ManualTest, ManualTestResult

        s = self.Session()
        try:
            s.add(ManualTest(id=1, project_id=1, type="Forced Entry",
                             required_option="Grade 40", finished=False))
            s.commit()
        finally:
            s.close()

        barrier = threading.Barrier(2)
        results, errors = [], []
        # How many times the builders ran in total. Two threads that never
        # collide run it twice; a real collision makes the loser build again.
        # Asserted below, so this test cannot pass because the race failed to
        # happen — which is the exact way the version it replaces was vacuous.
        builds = []

        def start(tag):
            session = self.Session()
            try:
                def build():
                    builds.append(tag)
                    n = next_attempt_number(session, "raced-test-id")
                    # Both threads hold this number before either inserts.
                    # Only the first try waits: a retry must be free to read the
                    # winner's committed row.
                    if not getattr(build, "waited", False):
                        build.waited = True
                        barrier.wait(timeout=10)
                    return ManualTestResult(
                        manual_test_id=1, trial_number=n,
                        labos_attempt_id=f"raced-{tag}-{n}",
                        labos_test_id="raced-test-id",
                        test_type="Forced Entry", status="In Progress",
                        labos_created_at=_now(), labos_updated_at=_now())

                obj = insert_attempt(session, build)
                session.commit()
                results.append(obj.trial_number)
            except Exception as exc:                          # noqa: BLE001
                errors.append(f"{tag}: {type(exc).__name__}: {exc}")
                session.rollback()
            finally:
                session.close()

        threads = [threading.Thread(target=start, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [],
                         "a lost race must be recovered by the retry loop, not "
                         "raised at the caller")
        self.assertEqual(sorted(results), [1, 2],
                         "the loser must take the next number, not the same one")
        self.assertGreater(
            len(builds), 2,
            f"the builders ran {len(builds)} times, so no collision occurred and "
            "this test proved nothing about the retry loop. The barrier is not "
            "forcing the race.")


class TheProductionSender(_Base):
    """What the real `service.send` does — the one path a fake sender hides.

    Every suite here and in `test_sync_concurrency` injects a transport that
    accepts any payload. That is right for testing the queue, and it is exactly
    why a defect in the *production* sender survived: it sent every phase
    through `upsert_records` without looking at `entry.phase`, so live, an
    attachment payload — which carries a `photo` object, not Airtable fields —
    would have been rejected as an unknown field. A 422 is terminal, so every
    photograph would have parked on first contact.

    So the production sender's own decisions get tested directly, with the
    client stubbed at the boundary rather than the sender replaced.
    """

    def _sender(self, calls):
        """**The production sender itself**, wired to a stub client.

        `service.make_sender(client, settings)` — the same function `main()`
        calls. This test used to hold a copy of that closure and assert, by
        scanning the source, that the copy still matched; a test of a
        resemblance rather than of the code. That shape is what let the
        phase-blind sender through in the first place, so the copy is gone.
        """
        from app.sync import service

        class StubClient:
            def upsert_records(self, table_id, records, **kw):
                calls.append((table_id, records))
                return {"records": [{"id": "recSTUB0000000001"}]}

        class StubSettings:
            results_table = "tblSTUB"

        return service.make_sender(StubClient(), StubSettings())

    def test_an_attachment_parks_with_a_truthful_reason_and_no_request(self):
        from app.sync import outbox, worker
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "g"})
        test_id = r.json()["id"]
        self.link_test("manual_tests", test_id)
        a = self.client.post(f"/projects/1/manual-tests/{test_id}/trials",
                             json={"operator_name": "technician-1"}).json()
        self.client.post(f"/test-results/{a['id']}/photos", files=_jpeg())

        calls = []
        s = self.Session()
        try:
            worker.drain(s, self._sender(calls))
            entries = {e.phase: e for e in s.query(outbox.SyncOutbox).all()}
        finally:
            s.close()

        self.assertEqual(entries["create"].state, "done")
        self.assertEqual(entries["attachment"].state, "parked",
                         "an unimplemented capability must park, not retry forever")
        self.assertIn("not implemented", entries["attachment"].last_error)
        # And no malformed request was ever made.
        self.assertEqual([r for _, recs in calls for r in recs
                          if "photo" in r], [],
                         "an attachment payload must never be sent as record fields")

    def test_a_parked_attachment_does_not_pin_the_headline_status(self):
        """§6: evidence delivery is tracked separately from the result."""
        from app.sync import outbox, worker
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "g"})
        test_id = r.json()["id"]
        self.link_test("manual_tests", test_id)
        a = self.client.post(f"/projects/1/manual-tests/{test_id}/trials",
                             json={"operator_name": "technician-1"}).json()
        self.client.post(f"/test-results/{a['id']}/photos", files=_jpeg())
        self.client.put(f"/test-results/{a['id']}/finish",
                        json={"result": True, "testing_continued": "Stopped"})
        self.client.put(f"/test-results/{a['id']}/verdict",
                        json={"test_result": "Pass", "verdict_by": "reviewer-1",
                              "retest_required": False})

        s = self.Session()
        try:
            worker.drain(s, self._sender([]))
            body = __import__("app.sync.state", fromlist=["state"]).status(s)
        finally:
            s.close()

        self.assertEqual(body["parked"], 0,
                         "no RECORD phase is parked, so the headline must not "
                         "say Retry Required and hide the next real failure")
        self.assertEqual(body["attachment_parked"], 1,
                         "but the stuck evidence must stay visible")
        self.assertGreaterEqual(body["attachment_backlog"], 1)


class ArtifactDeliveryIsTrackedPerPhotograph(_Base):
    """The discard bug, and the double-upload it would invite.

    Reported and reproduced by audit 2026-09-08: channels had separate queue
    heads but shared one delivery watermark, so once the verdict advanced
    `delivered_seq`, retrying an earlier parked photograph classified it
    superseded and marked it `done` **having never sent it**.
    """

    def _attempt_with_photo(self):
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "g"})
        test_id = r.json()["id"]
        self.link_test("manual_tests", test_id)
        a = self.client.post(f"/projects/1/manual-tests/{test_id}/trials",
                             json={"operator_name": "technician-1"}).json()
        self.client.post(f"/test-results/{a['id']}/photos", files=_jpeg())
        self.client.put(f"/test-results/{a['id']}/finish",
                        json={"result": True, "testing_continued": "Stopped"})
        self.client.put(f"/test-results/{a['id']}/verdict",
                        json={"test_result": "Pass", "verdict_by": "reviewer-1",
                              "retest_required": False})
        return a["labos_attempt_id"], a["id"]

    def test_an_undelivered_photo_survives_a_retry_after_the_verdict(self):
        from app.sync import outbox, worker
        aid, _ = self._attempt_with_photo()

        # Deliver everything except the attachment, which parks.
        s = self.Session()
        try:
            worker.drain(s, self._refusing_attachments())
            attach = (s.query(outbox.SyncOutbox)
                      .filter(outbox.SyncOutbox.attempt_id == aid,
                              outbox.SyncOutbox.phase == "attachment").one())
            self.assertEqual(attach.state, "parked")
            # The verdict has landed, so the shared watermark is past this entry.
            state = s.get(outbox.SyncAttemptState, aid)
            self.assertGreater(state.delivered_seq, attach.attempt_seq,
                               "the premise: the watermark is past the photo")

            # Repair it and retry with a sender that accepts.
            outbox.resume(s, attach.id)
            s.commit()
            sent = []
            worker.drain(s, lambda e: sent.append(e.phase) or "recPHOTO001")
            s.refresh(attach)
        finally:
            s.close()

        self.assertIn("attachment", sent,
                      "the retried photograph must actually be SENT, not "
                      "classified superseded and silently discarded")
        self.assertEqual(attach.state, "done")

    def test_a_delivered_photo_is_never_uploaded_twice(self):
        from app.sync import outbox, worker
        aid, pk = self._attempt_with_photo()
        s = self.Session()
        try:
            sent = []
            worker.drain(s, lambda e: sent.append(e.phase) or "recPHOTO001")
            first = sent.count("attachment")
            entry = (s.query(outbox.SyncOutbox)
                     .filter(outbox.SyncOutbox.attempt_id == aid,
                             outbox.SyncOutbox.phase == "attachment").one())
            photo_id = entry.payload["photo"]["id"]
            self.assertTrue(outbox.artifact_is_delivered(s, photo_id),
                            "a delivered photograph must be recorded as such")

            # Force a redelivery, as a lost response would.
            entry.state = outbox.PENDING
            entry.next_attempt_at = None
            s.commit()
            sent.clear()
            worker.drain(s, lambda e: sent.append(e.phase) or "recPHOTO001")
        finally:
            s.close()
        self.assertEqual(first, 1)
        self.assertEqual(sent.count("attachment"), 0,
                         "a photograph Airtable already holds must not be "
                         "attached a second time")

    def _refusing_attachments(self):
        from app.airtable.errors import AirtableValidationError
        from app.sync import outbox as ob

        def send(entry):
            if entry.phase == ob.ATTACHMENT:
                raise AirtableValidationError("uploader not built")
            return "recRECORD0001"
        return send


class FailedPublicationIsVisibleAndRepairable(_Base):
    """A refused payload used to read `Synced` and had nothing to retry."""

    def _linked_but_unbuildable(self):
        """A linked attempt whose terminal payload the envelope will refuse.

        Finishing without `testing_continued` leaves a terminal-required field
        absent, which is a refusal on our side rather than an Airtable problem —
        exactly the class of failure that had no queue entry.
        """
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "g"})
        test_id = r.json()["id"]
        self.link_test("manual_tests", test_id)
        a = self.client.post(f"/projects/1/manual-tests/{test_id}/trials",
                             json={"operator_name": "technician-1"}).json()
        self.client.put(f"/test-results/{a['id']}/finish", json={"result": True})
        return a["labos_attempt_id"], a["id"]

    def test_a_refused_payload_is_recorded_and_shows_in_status(self):
        aid, _ = self._linked_but_unbuildable()
        body = self.client.get("/sync/status").json()
        self.assertGreaterEqual(body["failed_publications"], 1,
                                "a refused publication must be counted")
        self.assertEqual(body["status"], "Retry Required",
                         "it must NOT read Synced — nothing is queued, and that "
                         "is exactly why it was invisible before")

        failures = self.client.get("/sync/failures").json()
        self.assertGreaterEqual(failures["count"], 1)
        row = [f for f in failures["failures"] if f["attempt_id"] == aid][0]
        self.assertEqual(row["phase"], "terminal")
        self.assertTrue(row["recoverable"])
        self.assertIn("Testing Continued", row["error"])

    def test_repair_requeues_once_the_data_is_fixed_and_409s_before(self):
        aid, pk = self._linked_but_unbuildable()
        fid = [f for f in self.client.get("/sync/failures").json()["failures"]
               if f["attempt_id"] == aid][0]["id"]

        # Still broken: repair must refuse rather than queue a bad payload.
        again = self.client.post(f"/sync/failures/{fid}/repair")
        self.assertEqual(again.status_code, 409, again.text)
        self.assertIn("Still refused", again.json()["detail"])

        # Fix the data the way an operator would, then repair.
        s = self.Session()
        try:
            from app.data.models import TestResult
            row = s.query(TestResult).filter(
                TestResult.labos_attempt_id == aid).one()
            row.testing_continued = "Stopped"
            s.commit()
        finally:
            s.close()

        ok = self.client.post(f"/sync/failures/{fid}/repair")
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["queued_phase"], "terminal")

        phases = [e[0] for e in self.queue(aid, channel="record")]
        self.assertEqual(phases, ["create", "terminal"])
        self.assertEqual(self.client.get("/sync/status").json()
                         ["failed_publications"], 0,
                         "a repaired failure must stop counting")

    def test_an_incomplete_binding_is_recorded_as_not_repairable(self):
        """No retry fixes a missing protocol id — say so instead of looping."""
        r = self.client.post("/projects/1/manual-tests/",
                             json={"type": "Forced Entry", "required_option": "g"})
        # deliberately NOT linked: the project has ids, the test does not
        a = self.client.post(f"/projects/1/manual-tests/{r.json()['id']}/trials",
                             json={"operator_name": "technician-1"}).json()
        rows = [f for f in self.client.get("/sync/failures").json()["failures"]
                if f["attempt_id"] == a["labos_attempt_id"]]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["recoverable"])
        fid = rows[0]["id"]

        # Repair re-checks rather than trusting the stored flag: while the
        # binding is still missing it 409s and says what to do.
        r2 = self.client.post(f"/sync/failures/{fid}/repair")
        self.assertEqual(r2.status_code, 409)
        self.assertIn("Bind this job", r2.json()["detail"])

        # **And it resolves once the job is bound — the result is not stranded.**
        # A stored "not recoverable" must not permanently unpublish a real
        # attempt that keeps its measurements and its evidence.
        self.link_test("manual_tests", 1)
        r3 = self.client.post(f"/sync/failures/{fid}/repair")
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertEqual(r3.json()["queued_phase"], "create")


class RigTypesCompleteTheirLifecycle(_Base):
    """Static and Cycles through every phase, via the real routes."""

    def _rig_test(self, kind):
        table = "static_tests" if kind == "static" else "cyclic_tests"
        s = self.Session()
        try:
            if kind == "static":
                s.execute(sa.text(
                    "INSERT INTO static_tests (id, finished, index, "
                    "pressure_factor, pressure, duration, type, preset, "
                    "project_id, airtable_protocol_id, airtable_section_id, "
                    "airtable_section_name) VALUES (1, false, 0, '0.75', 45.0, "
                    "10, 'static', false, 1, :p, :sec, 'DP (+) (PSF)')"),
                    {"p": REC["protocol"], "sec": REC["section"]})
            else:
                s.execute(sa.text(
                    "INSERT INTO cyclic_tests (id, finished, index, type, "
                    "cycles, low_pressure, high_pressure, resume, current_cycle,"
                    " preset, project_id, airtable_protocol_id, "
                    "airtable_section_id, airtable_section_name) VALUES "
                    # current_cycle 1000: the rig has run the stage, which is
                    # what `Cycles Completed` reports. 0 would mean it ran none.
                    "(1, false, 0, 'cyclic', 1000, 30.0, 60.0, false, 1000, "
                    "false, 1, :p, :sec, 'DP (+) (PSF)')"),
                    {"p": REC["protocol"], "sec": REC["section"]})
            s.commit()
        finally:
            s.close()
        return "static_tests" if kind == "static" else "cyclic-tests"

    def _attempt_id(self):
        from app.data.models import TestResult
        s = self.Session()
        try:
            return s.query(TestResult).one().labos_attempt_id
        finally:
            s.close()

    def _post_trial(self, kind, body):
        path = ("static_tests" if kind == "static" else "cyclic-tests")
        return self.client.post(f"/projects/1/{path}/0/trials", json=body)

    def _check(self, kind):
        self._rig_test(kind)
        r = self._post_trial(kind, {
            "deflections": [{"deflection_gauge": "g1", "max_deflection": 1234.0,
                             "permanent_deflection": 12.0, "recovery": 60.0}],
            "operator_name": "technician-1", "result": True,
            "testing_continued": "Stopped"})
        self.assertIn(r.status_code, (200, 201), r.text)
        # The rig response schema does not expose `labos_attempt_id`, so read it
        # from the row rather than asserting against None — which would have
        # made `queue(None)` return every entry and passed by accident.
        aid = self._attempt_id()
        phases = [e[0] for e in self.queue(aid, channel="record")]
        self.assertEqual(phases, ["create", "terminal"],
                         f"{kind}: a rig stage is posted finished, so it must "
                         "reach terminal in the same call")
        return aid

    def test_both_initialisation_paths_agree_on_retest_required(self):
        """One rule, two creation paths — pinned so they cannot diverge again.

        `attempts.begin()` (static, cyclic) stamped `retest_required = False`
        while `_start_attempt` (manual, impact) left it NULL. §6 forbids the
        first outright, and the practical effect was that the envelope's phase
        guard refused **every rig terminal write**, so those two types never
        queued a terminal at all.
        """
        from app.data import attempts
        kwargs = attempts.begin([], test_type="Static Load",
                                kind=attempts.STATIC, parent_id=1)
        self.assertNotIn("retest_required", kwargs,
                         "creation must not answer a question only a reviewer "
                         "can answer")

    def test_static_reaches_terminal(self):
        self._check("static")

    def test_cyclic_reaches_terminal(self):
        self._check("cyclic")

    def test_run_start_operator_completes_a_deflections_only_callback(self):
        """**No firmware change is needed**, and this is why.

        The rig sends `deflections` alone. The operator is not a fact the rig
        has — it is declared by a person at the screen when they begin the run
        (`identity_assurance = declared`, contract §4). Captured at run start and
        inherited by the callback, the attempt is completable with the wire
        contract untouched.
        """
        self._rig_test("static")
        started = self.client.put("/projects/1/static_tests/0/start",
                                  json={"operator_name": "technician-1"})
        self.assertEqual(started.status_code, 200, started.text)

        # Exactly what production firmware posts today: deflections, nothing else.
        r = self._post_trial("static", {
            "deflections": [{"deflection_gauge": "g1", "max_deflection": 1234.0,
                             "permanent_deflection": 12.0, "recovery": 60.0}]})
        self.assertIn(r.status_code, (200, 201), r.text)
        aid = self._attempt_id()
        self.assertEqual([e[0] for e in self.queue(aid, channel="record")],
                         ["create", "terminal"],
                         "a deflections-only callback must complete when the "
                         "operator was declared at run start")

        from app.data.models import TestResult
        s = self.Session()
        try:
            row = s.query(TestResult).filter(
                TestResult.labos_attempt_id == aid).one()
            self.assertEqual(row.operator_name, "technician-1")
        finally:
            s.close()

    def test_cycles_completed_is_rig_evidence_snapshotted_not_the_target(self):
        """Where the number comes from, and why it is captured not read.

        `cyclic_tests.cycles` is the configured **target** (1000 here) and is
        never published as an achievement. `current_cycle` is progress the rig
        reports during the run via `PUT .../update_status`. That is the source.

        It is snapshotted onto the attempt at termination because `current_cycle`
        lives on the test and is zeroed by both `reset` and `finish` — so reading
        it at publish time, which was the first fix, would find the evidence
        already gone or overwritten by a later run.
        """
        import json as _json
        self._rig_test("cyclic")
        s = self.Session()
        try:
            # The rig has run 640 of the 1000 configured cycles.
            s.execute(sa.text("UPDATE cyclic_tests SET current_cycle=640, "
                              "operator_name='technician-1' WHERE id=1"))
            s.commit()
        finally:
            s.close()

        r = self._post_trial("cyclic", {
            "deflections": [{"deflection_gauge": "g1", "max_deflection": 9.0,
                             "permanent_deflection": 1.0, "recovery": 60.0}]})
        self.assertIn(r.status_code, (200, 201), r.text)
        aid = self._attempt_id()

        terminal = [e for e in self.queue(aid, channel="record")
                    if e[0] == "terminal"]
        self.assertEqual(len(terminal), 1, "cyclic must reach terminal")
        payload = terminal[0][3]
        # `Cycles Completed` is one of the nine JSON-only fields (§10.15): it has
        # no Airtable column by decision, so the envelope routes it into the JSON
        # valve. Asserting a payload key would be asserting a field that is
        # deliberately absent.
        self.assertNotIn("Cycles Completed", payload)
        detail = _json.loads(payload["Complete LabOS JSON Response"])
        self.assertEqual(detail["labos_extra"].get("cycles_completed"), 640,
                         "the rig-reported count, not the 1000 target")
        self.assertEqual(detail["requirements"].get("cycles_required"), 1000,
                         "the target belongs in the requirements snapshot, "
                         "never as an achievement")

        # Now zero it the way `finish` does. The published evidence must survive.
        s = self.Session()
        try:
            s.execute(sa.text("UPDATE cyclic_tests SET current_cycle=0 WHERE id=1"))
            s.commit()
            from app.data.models import TestResult
            row = s.query(TestResult).filter(
                TestResult.labos_attempt_id == aid).one()
            self.assertEqual(row.cycles_completed, 640,
                             "a snapshot survives the test being reset; a live "
                             "read would now report nothing")
        finally:
            s.close()

    def test_without_an_operator_it_stays_open_and_is_recorded(self):
        """LabOS does not invent an operator, and does not hide the refusal.

        This is what production firmware does today: it posts `deflections`
        alone. The attempt stays In Progress, no terminal is queued, and the
        reason is visible — rather than the silence that made this look fine.
        """
        self._rig_test("static")
        r = self._post_trial("static", {
            "deflections": [{"deflection_gauge": "g1", "max_deflection": 1.0,
                             "permanent_deflection": 0.0, "recovery": 60.0}]})
        self.assertIn(r.status_code, (200, 201), r.text)
        aid = self._attempt_id()
        self.assertEqual([e[0] for e in self.queue(aid, channel="record")],
                         ["create"])
        from app.data.models import TestResult
        s = self.Session()
        try:
            row = s.query(TestResult).filter(
                TestResult.labos_attempt_id == aid).one()
            self.assertEqual(row.status, "In Progress")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
