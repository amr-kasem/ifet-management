"""Every one of the five test types survives every delivery phase.

Delivery plan §6.0 step 1 — the gate on sending the change document to the
Airtable team. It is a schema-coverage test, not a behaviour test: the question
it answers is "for each of Static Load, Cycles, Impact, Forced Entry and ANSI
Z97.1, can LabOS actually build the payloads the contract says it sends?"

Run as a suite on 2026-09-07 for the first time, it found three defects that
every existing test missed, because each existing test happened to use Static
Load and the create phase:

  1. `Test Result = Pending` at create — mandated by contract §6, refused by
     the envelope, and not even a legal option in `contract.py`.
  2. `Test Date` stamped with the **start** instant, and the two dateTime
     columns applied to the base on 2026-09-06 never written at all.
  3. `LabOS Photos` required inside the terminal payload for the three manual
     types, which made an attachment upload a precondition for publishing a
     result — the opposite of the separate delivery channel §6 specifies.

The live option sets come from `tests.fake_schema`, which mirrors the base, so
this runs offline. `app.airtable.probe` is what checks the mirror is still true.
"""

import datetime as dt
import json
import unittest

from app.airtable import contract as C
from app.airtable.envelope import (
    EnvelopeError, build_start, build_terminal, build_verdict,
    options_from_snapshot,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 7, 13, 0, 0, tzinfo=UTC)
T1 = dt.datetime(2026, 9, 7, 14, 30, 0, tzinfo=UTC)


def live_options():
    from app.airtable import probe
    from tests import fake_schema as FS
    return options_from_snapshot(probe.build_snapshot(FS.schema()))


def identity(test_type):
    """The always-required set — the same for all five types, by design."""
    return {
        "Airtable Project ID": "recPrj0000000001",
        "Airtable Mock-Up ID": "recMck0000000001",
        "Airtable Protocol ID": "recPro0000000001",
        "Airtable Section ID": "recSec0000000001",
        "LabOS Test ID": "b0e1-test-uuid",
        "LabOS Attempt ID": "a1f2-attempt-uuid",
        "Attempt Number": 1,
        "Test Type": test_type,
        "Test Name": f"{test_type} — specimen A",
        "Testing Start Date": T0,
        "LabOS Created At": T0,
        "LabOS Updated At": T0,
    }


# What each type adds at terminal beyond the common set. Static Load adds
# nothing: A2 omits Measured Value and Max Pressure Achieved, and A3 quarantines
# the deflection pair, so a completed rig row carries no number describing what
# physically happened. That is the decided initial behaviour (plan §4.4).
TERMINAL_EXTRA = {
    C.STATIC_LOAD: {},
    C.CYCLES: {"Cycles Completed": 4500},
    # Free text on the wire, but shaped like what `_impact_result` produces —
    # one attempt is one impact since §4.5a, so "2/2 shots" describes a model
    # that no longer exists and would mislead the next reader of this fixture.
    # Since 2026-09-10 Impact's terminal write also carries what the attempt
    # ran under. Both are guaranteed by the completion gate in
    # `finish_attempt`, so requiring them on the wire cannot strand an attempt
    # the API would have let finish.
    C.IMPACT: {"Impact Result": "Pass - impact 2 resisted",
               "Impact Classification": "LMI Level D",
               "Target Impact Velocity": 50.0,
               "LabOS Photos": [{"url": "https://labos.example/p.jpg"}]},
    # Since 2026-09-11 each manual type also carries its own standard's
    # result. Pending at terminal, exactly like Test Result, because it is the
    # same value projected by type rather than a second lifecycle.
    C.FORCED_ENTRY: {"Forced Entry Result": C.RESULT_PENDING},
    C.ANSI_Z97: {"ANSI Result": C.RESULT_PENDING},
}


def terminal(test_type, **over):
    v = identity(test_type)
    v.update({
        "LabOS Updated At": T1,
        "Operator Name": "technician-1",
        "Testing Continued": "Stopped",
        "Testing End Date": T1,
        "Test Result": C.RESULT_PENDING,
        "Result Detail (JSON)": {"stages": []},
    })
    v.update(TERMINAL_EXTRA[test_type])
    v.update(over)
    return v


def reviewed(test_type, **over):
    """A first-review payload — §6 writes these four together, once."""
    v = terminal(test_type)
    v.update({
        "Test Result": "Pass",
        "LabOS Verdict By": "reviewer-1",
        "LabOS Verdict At": T1,
        "Retest Required": False,
    })
    # The dedicated field moves with the verdict, because it is the same value.
    if test_type == C.FORCED_ENTRY:
        v["Forced Entry Result"] = "Pass"
    if test_type == C.ANSI_Z97:
        v["ANSI Result"] = "Pass"
    v.update(over)
    return v


class AllFiveTypesBuild(unittest.TestCase):
    """The coverage claim itself: no type is a special case that was skipped."""

    def setUp(self):
        self.live = live_options()

    def test_the_contract_knows_exactly_five(self):
        self.assertEqual(len(C.TEST_TYPES), 5)
        self.assertEqual(set(C.TEST_TYPES), set(C.REQUIRED_BY_TEST_TYPE))

    def test_create_builds_for_every_type(self):
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                w = build_start(identity(tt), live_options=self.live)
                self.assertEqual(w["Test Type"], tt)
                self.assertEqual(w["Test Status"], "In Progress")

    def test_terminal_builds_for_every_type(self):
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                w = build_terminal(terminal(tt), live_options=self.live)
                self.assertEqual(w["Test Status"], "Completed")

    def test_verdict_builds_for_every_type(self):
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                w = build_verdict(reviewed(tt), live_options=self.live)
                self.assertEqual(w["Test Result"], "Passed")   # their spelling
                self.assertEqual(w["LabOS Verdict By"], "reviewer-1")

    def test_terminal_stays_pending_for_every_type(self):
        """§6 — the measurement lands before anyone judges it."""
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                w = build_terminal(terminal(tt), live_options=self.live)
                self.assertEqual(w["Test Result"], "Pending")
                self.assertNotIn("LabOS Verdict By", w)
                self.assertNotIn("Retest Required", w)

    def test_a_verdict_at_terminal_is_refused_for_every_type(self):
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                with self.assertRaises(EnvelopeError):
                    build_terminal(terminal(tt, **{"Test Result": "Pass"}),
                                   live_options=self.live)

    def test_retest_required_is_never_inferred_at_terminal(self):
        """§6 — meaningful only once review exists, never an unchecked box."""
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                with self.assertRaises(EnvelopeError):
                    build_terminal(terminal(tt, **{"Retest Required": False}),
                                   live_options=self.live)

    def test_a_review_cannot_land_on_a_running_attempt(self):
        with self.assertRaises(EnvelopeError):
            build_verdict(reviewed(C.STATIC_LOAD), status=C.IN_PROGRESS,
                          live_options=self.live)

    def test_aborted_builds_for_every_type(self):
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                w = build_terminal(
                    terminal(tt, **{"Abort Reason": "Equipment Fault"}),
                    status=C.ABORTED, live_options=self.live)
                self.assertEqual(w["Test Status"], "Abborted")  # §10.18


class PendingAtCreate(unittest.TestCase):
    """§6 — creation explicitly sends Test Result = Pending."""

    def setUp(self):
        self.live = live_options()

    def test_pending_is_defaulted_for_every_type(self):
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                w = build_start(identity(tt), live_options=self.live)
                self.assertEqual(w["Test Result"], C.RESULT_PENDING)

    def test_pending_is_an_option_the_live_base_offers(self):
        self.assertIn(C.RESULT_PENDING,
                      C.BY_LABOS_NAME["Test Result"].options)
        self.assertIn(C.RESULT_PENDING, self.live["Test Result"])

    def test_a_verdict_at_create_is_refused(self):
        for verdict in ("Pass", "Fail", "Inconclusive"):
            with self.subTest(verdict):
                v = identity(C.STATIC_LOAD)
                v["Test Result"] = verdict
                with self.assertRaises(EnvelopeError):
                    build_start(v, live_options=self.live)

    def test_not_applicable_is_never_ours_to_send(self):
        """§4 reserves it for Airtable's handling of work LabOS never ran."""
        self.assertNotIn("Not Applicable",
                         C.BY_LABOS_NAME["Test Result"].options)


class Dates(unittest.TestCase):
    """§5 — Test Date is completion; the start/end pair has real columns."""

    def setUp(self):
        self.live = live_options()

    def test_completion_instant_for_every_type(self):
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                w = build_terminal(terminal(tt), live_options=self.live)
                self.assertEqual(w["Test Date"], "2026-09-07T14:30:00Z")
                self.assertEqual(w["Testing Start Date"], "2026-09-07T13:00:00Z")
                self.assertEqual(w["Testing End Date"], "2026-09-07T14:30:00Z")

    def test_omitted_while_running_for_every_type(self):
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                self.assertNotIn("Test Date",
                                 build_start(identity(tt), live_options=self.live))


class EvidenceIsNotADeliveryPrecondition(unittest.TestCase):
    """§6 — attachments are their own channel and may settle after terminal.

    Impact requires photographs; Forced Entry and ANSI Z97.1 are recorded as a
    pass/fail outcome and require none (IFET, 2026-09-07). The requirement that
    does exist lives on the run-finish path, not in the terminal payload, so a
    queued upload can never stop a measured result from reaching Airtable.
    """

    def setUp(self):
        self.live = live_options()

    def test_impact_is_the_only_type_requiring_photographs(self):
        self.assertEqual(set(C.REQUIRED_EVIDENCE_BY_TEST_TYPE), {C.IMPACT})
        self.assertEqual(C.REQUIRED_EVIDENCE_BY_TEST_TYPE[C.IMPACT],
                         ("LabOS Photos",))

    def test_no_type_requires_an_attachment_in_the_terminal_payload(self):
        for tt, required in C.REQUIRED_BY_TEST_TYPE.items():
            with self.subTest(tt):
                self.assertNotIn("LabOS Photos", required)

    def test_terminal_publishes_while_the_upload_is_still_queued(self):
        for tt in C.TEST_TYPES:
            with self.subTest(tt):
                v = terminal(tt)
                v.pop("LabOS Photos", None)
                w = build_terminal(v, live_options=self.live)
                self.assertEqual(w["Test Status"], "Completed")


class QuarantinedNumbersStayOut(unittest.TestCase):
    """A2/A3 — refused in the JSON as well as in the columns, for every type."""

    def setUp(self):
        self.live = live_options()

    def test_every_type_refuses_the_three_omitted_fields(self):
        for tt in C.TEST_TYPES:
            for field, value in (("Max Pressure Achieved", 61.2),
                                 ("Deflection Value", 0.31),
                                 ("Deflection Unit", "in")):
                with self.subTest(tt=tt, field=field):
                    with self.assertRaises(EnvelopeError):
                        build_terminal(terminal(tt, **{field: value}),
                                       live_options=self.live)


class JsonOnlyFieldsSurvive(unittest.TestCase):
    """§10.15 — nine fields have no column and ride in the JSON valve.

    Named here so a register-vs-base diff that reports them "missing" has
    somewhere to point. They are absent by decision, not by oversight.
    """

    JSON_ONLY = ("Test Name", "Abort Reason", "Required Value", "Required Unit",
                 "Cycles Required", "Cycles Completed", "Test Rig",
                 "LabOS Version", "Result Rationale")

    def test_the_set_is_exactly_these_nine(self):
        # `pending_schema` fields are excluded deliberately: they are ABSENT
        # for the other reason — decided and not yet created — and they become
        # PRESENT when the Testing Base write lands. A JSON-only field never
        # does. Conflating the two would make this assertion change every time
        # a field is applied.
        absent = tuple(f.labos_name for f in C.FIELDS
                       if not f.expected_live and not f.pending_schema)
        self.assertEqual(absent, self.JSON_ONLY)

    def test_the_pending_schema_set_is_what_is_currently_owed(self):
        """`pending_schema` marks a field decided but not yet created. TA7b's
        two were created on 2026-09-11 and left the set; TA6's two entered it
        the same day and leave when they are applied."""
        self.assertEqual(("Forced Entry Result", "ANSI Result"),
                         tuple(f.labos_name for f in C.FIELDS if f.pending_schema))

    def test_cycles_completed_reaches_the_json_not_a_column(self):
        w = build_terminal(terminal(C.CYCLES), live_options=live_options())
        self.assertNotIn("Cycles Completed", w)
        blob = json.loads(w["Complete LabOS JSON Response"])
        self.assertEqual(blob["labos_extra"]["cycles_completed"], 4500)


if __name__ == "__main__":
    unittest.main()
