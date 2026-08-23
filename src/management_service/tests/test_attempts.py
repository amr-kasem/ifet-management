"""P1 / Ref 46 — attempt identity and lifecycle (contract §3, §3.1)."""

import datetime as dt
import unittest

from app.data import attempts

UTC = dt.timezone.utc


class Stub:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def sibling(test_id):
    return Stub(labos_test_id=test_id)


def live_attempt(**over):
    d = dict(labos_attempt_id="a-1", status=attempts.IN_PROGRESS, terminal_at=None,
             test_result=None, abort_reason=None, testing_continued=None,
             testing_end_date=None, labos_updated_at=None,
             corrects_attempt_id=None, correction_reason=None)
    d.update(over)
    return Stub(**d)


class TestIdentity(unittest.TestCase):
    def test_first_attempt_mints_a_test_id(self):
        self.assertTrue(attempts.test_id_for([]))

    def test_later_attempts_share_the_first_ones_test_id(self):
        """The failure mode this guards: every attempt minting its own test id
        looks completely valid and makes retest counting impossible."""
        self.assertEqual(attempts.test_id_for([sibling("t-1")]), "t-1")

    def test_a_backfilled_historical_sibling_still_counts(self):
        """A re-run of an old test joins its existing group rather than
        starting a new one."""
        self.assertEqual(attempts.test_id_for([sibling(None), sibling("t-old")]), "t-old")

    def test_every_attempt_gets_a_distinct_merge_key(self):
        keys = {attempts.begin([], test_type="Static Load")["labos_attempt_id"]
                for _ in range(50)}
        self.assertEqual(len(keys), 50)

    def test_new_attempts_are_pending_not_excluded(self):
        """Excluded is only ever set by the migration, on pre-integration rows."""
        self.assertEqual(attempts.begin([], test_type="Static Load")["airtable_sync_state"],
                         attempts.PENDING)

    def test_begins_in_progress_with_aware_timestamps(self):
        b = attempts.begin([], test_type="Static Load")
        self.assertEqual(b["status"], attempts.IN_PROGRESS)
        self.assertIsNotNone(b["testing_start_date"].tzinfo)
        self.assertIs(b["retest_required"], False)


class Lifecycle(unittest.TestCase):
    def test_completing_stamps_the_terminal_lock(self):
        a = attempts.mark_terminal(live_attempt(), attempts.COMPLETED, test_result="Pass")
        self.assertEqual(a.status, "Completed")
        self.assertIsNotNone(a.terminal_at)
        self.assertEqual(a.testing_end_date, a.terminal_at)

    def test_a_terminal_attempt_cannot_be_moved_again(self):
        """§3 — the terminal state is final. A second transition would destroy
        the evidence a report was issued against."""
        a = attempts.mark_terminal(live_attempt(), attempts.COMPLETED, test_result="Pass")
        with self.assertRaises(ValueError) as ctx:
            attempts.mark_terminal(a, attempts.COMPLETED, test_result="Fail")
        self.assertIn("already terminal", str(ctx.exception))
        self.assertIn("corrects_attempt_id", str(ctx.exception))

    def test_non_terminal_status_is_refused(self):
        with self.assertRaises(ValueError):
            attempts.mark_terminal(live_attempt(), "In Progress")


class Corrections(unittest.TestCase):
    def test_a_correction_names_what_it_supersedes(self):
        a = attempts.as_correction(live_attempt(labos_attempt_id="a-2"), "a-1",
                                   "sensor unit misconfiguration (PSI logged as PSF)")
        self.assertEqual(a.corrects_attempt_id, "a-1")

    def test_a_correction_must_state_why(self):
        for reason in (None, "", "   "):
            with self.assertRaises(ValueError):
                attempts.as_correction(live_attempt(labos_attempt_id="a-2"), "a-1", reason)

    def test_an_attempt_cannot_supersede_itself(self):
        with self.assertRaises(ValueError):
            attempts.as_correction(live_attempt(labos_attempt_id="a-1"), "a-1", "typo")

    def test_a_retest_is_simply_a_correction_without_the_reference(self):
        """§3.1 — the reference field is the ONLY thing distinguishing them."""
        retest = live_attempt(labos_attempt_id="a-2")
        self.assertIsNone(retest.corrects_attempt_id)


if __name__ == "__main__":
    unittest.main()
