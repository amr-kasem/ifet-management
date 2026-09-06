"""The §7.1 guarantees, against real Postgres.

Scoped deliberately, after the 2026-09-06 discussion about what is actually
concurrent in this system:

**The rigs are not.** One rig runs one test at a time - a hardware limit. Two
rigs run two *different* attempts, so they take different `attempt_id`s and never
contend for the same outbox row. There is no "two rigs at once" case to test and
this module does not pretend otherwise.

**Two things are.**

1.  *One attempt, two actors.* The rig POSTs its `/trials` callback for attempt A
    while the operator uploads a photo or presses finish for A from the UI. Both
    enqueue into A's queue. A human and a machine, and no hardware limit between
    them. That is the case `enqueue`'s savepoint retry exists for.

2.  *The worker count is a deployment property, not a hardware one.* The decision
    is one worker, **enforced** - so the first test here is that a second one
    refuses to start, and the fencing tests are what make a violation harmless
    if the enforcement is ever bypassed.

Every case needs Postgres. On SQLite `FOR UPDATE SKIP LOCKED` is silently
ignored, advisory locks do not exist, and a savepoint retry has nothing to race,
so passing there would prove nothing. They skip rather than lie.
"""

import datetime as dt
import threading
import unittest

try:
    from sqlalchemy.exc import IntegrityError  # noqa: F401
except ImportError as exc:                                  # pragma: no cover
    raise unittest.SkipTest(f"sqlalchemy not installed: {exc}") from exc

from app.airtable import client as airtable_client
from app.sync import outbox, singleton
from app.sync import worker as sync_worker
from tests import pg_support

T0 = dt.datetime(2026, 9, 6, 12, 0, 0, tzinfo=dt.timezone.utc)


def at(seconds):
    return T0 + dt.timedelta(seconds=seconds)


class PgCase(unittest.TestCase):
    """One engine per test, torn down, tables truncated."""

    @classmethod
    def setUpClass(cls):
        if not pg_support.is_postgres():
            raise unittest.SkipTest(
                "needs Postgres - set M2_DATABASE_URL (tests/postgres_harness/)")

    def setUp(self):
        self.engine = pg_support.make_engine()
        pg_support.prepare(self.engine)
        self.Session = pg_support.session_factory(self.engine)

    def tearDown(self):
        self.engine.dispose()


# ------------------------------------------------------- one worker, enforced


class OneWorkerEnforced(PgCase):
    def test_a_second_worker_refuses_to_start(self):
        """The decision is one worker. This is what makes it true."""
        with singleton.acquire(self.engine, required=True) as first:
            self.assertTrue(first.enforced)
            self.assertEqual(singleton.holder_count(self.engine), 1)

            with self.assertRaises(singleton.WorkerAlreadyRunning):
                singleton.acquire(self.engine, required=True)

            # The refusal must not have disturbed the incumbent.
            self.assertEqual(singleton.holder_count(self.engine), 1)

    def test_the_slot_frees_itself_when_the_holder_dies(self):
        """No TTL, no stale lock file: the lock lives on the connection.

        This is why an advisory lock beats a row in a table for this job. A
        SIGKILLed worker leaves nothing behind to clean up.
        """
        slot = singleton.acquire(self.engine, required=True)
        self.assertEqual(singleton.holder_count(self.engine), 1)

        slot.release()          # stands in for the process dying
        self.assertEqual(singleton.holder_count(self.engine), 0)

        # And the slot is immediately takeable again - no cooldown.
        with singleton.acquire(self.engine, required=True):
            self.assertEqual(singleton.holder_count(self.engine), 1)

    def test_an_unenforceable_slot_is_refused_when_enforcement_is_required(self):
        """A worker that cannot enforce the guarantee must not run silently."""
        sqlite_engine = pg_support.create_engine("sqlite://")
        try:
            with self.assertRaises(singleton.WorkerAlreadyRunning):
                singleton.acquire(sqlite_engine, required=True)
            # ...but the unit-test path may opt out explicitly.
            slot = singleton.acquire(sqlite_engine, required=False)
            self.assertFalse(slot.enforced)
            slot.release()
        finally:
            sqlite_engine.dispose()


# --------------------------------------------- one attempt, two actors


class SameAttemptTwoActors(PgCase):
    """The realistic enqueue race: the rig's callback and the operator's UI.

    Run in two threads on two connections, with a barrier between reading
    `max(seq)` and inserting, so both genuinely start from the same number. A
    single-threaded version of this **deadlocks** and that is not a bug in the
    code: the second INSERT waits on the unique index until the first
    transaction commits, and in one thread it never does. Concurrency has to be
    tested concurrently.
    """

    @staticmethod
    def _sync_once(barrier, record=None):
        """A seam hook that meets the barrier on the FIRST read only.

        `_after_read` fires on every retry iteration, so a hook that always
        waits leaves the retrying thread alone at a two-party barrier and breaks
        it. Rendezvous once - which is all the race needs - then get out of the
        way so the retry can proceed.
        """
        met = []

        def hook(seq):
            if record is not None:
                record.append(seq)
            if not met:
                met.append(True)
                barrier.wait()

        return hook

    def _run_both(self, phases):
        ready = threading.Barrier(len(phases), timeout=20)
        outcomes = {}
        errors = []

        def actor(phase):
            session = self.Session()
            try:
                entry = outbox.enqueue(
                    session, "A", phase, {"LabOS Attempt ID": "A", "phase": phase},
                    # Both threads reach here holding the same next_seq, so one
                    # of them must collide and retry.
                    _after_read=self._sync_once(ready),
                )
                session.commit()
                outcomes[phase] = entry.attempt_seq
            except Exception as exc:                        # pragma: no cover
                errors.append((phase, exc))
                session.rollback()
            finally:
                session.close()

        threads = [threading.Thread(target=actor, args=(p,)) for p in phases]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "an enqueue never finished")
        return outcomes, errors

    def test_rig_callback_and_operator_photo_both_land(self):
        """Neither caller's transaction fails, and the two get distinct places.

        Before the savepoint, both computed the same `max(seq)+1`; the loser's
        unique-constraint violation aborted its whole transaction, so the
        operator's save failed to protect a queue whose entire purpose is that
        the save never fails.
        """
        outcomes, errors = self._run_both([outbox.TERMINAL, outbox.ATTACHMENT])

        self.assertEqual(errors, [], "no caller's transaction may fail")
        self.assertEqual(sorted(outcomes.values()), [1, 2],
                         "the two actors take distinct, dense positions")

        session = self.Session()
        try:
            rows = (session.query(outbox.SyncOutbox)
                    .filter(outbox.SyncOutbox.attempt_id == "A")
                    .order_by(outbox.SyncOutbox.attempt_seq).all())
            self.assertEqual(len(rows), 2, "both actors' entries must survive")
            self.assertEqual({r.phase for r in rows},
                             {outbox.TERMINAL, outbox.ATTACHMENT})
        finally:
            session.close()

    def test_the_loser_retried_rather_than_failing(self):
        """Proves the retry actually fired, not that the race failed to happen."""
        seen = []
        ready = threading.Barrier(2, timeout=20)
        errors = []

        def actor(phase):
            session = self.Session()
            try:
                outbox.enqueue(
                    session, "B", phase, {"LabOS Attempt ID": "B"},
                    _after_read=self._sync_once(ready, record=seen),
                )
                session.commit()
            except Exception as exc:                        # pragma: no cover
                errors.append(exc)
                session.rollback()
            finally:
                session.close()

        threads = [threading.Thread(target=actor, args=(p,))
                   for p in (outbox.TERMINAL, outbox.ATTACHMENT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        # Two reads of seq 1 (the collision), then a third read of 2 by whichever
        # thread lost and went round again. Without the savepoint retry there is
        # no third read - there is an aborted transaction.
        self.assertEqual(seen.count(1), 2, "both actors read the same next_seq")
        self.assertIn(2, seen, "the loser re-read and retried")


# ------------------------------------------------------ exclusive claim


class ExclusiveClaim(PgCase):
    """`FOR UPDATE SKIP LOCKED` - insurance, and cheap at one worker."""

    def setUp(self):
        super().setUp()
        session = self.Session()
        try:
            for aid in ("A", "B"):
                outbox.enqueue(session, aid, outbox.CREATE,
                               {"LabOS Attempt ID": aid})
            session.commit()
        finally:
            session.close()

    def test_two_claimers_never_hold_the_same_entry(self):
        """Two open transactions, two separate connections, no overlap."""
        one = self.Session()
        two = self.Session()
        try:
            first = outbox.claim(one, now=T0)
            second = outbox.claim(two, now=T0)

            first_ids = {e.id for e in first}
            second_ids = {e.id for e in second}
            self.assertTrue(first_ids, "the first claimer must get work")
            self.assertEqual(first_ids & second_ids, set(),
                             "an entry must never be held by two claimers")
            # Together they cover both queues: SKIP LOCKED redirects the second
            # claimer to different work rather than blocking or erroring.
            self.assertEqual(len(first_ids | second_ids), 2)
        finally:
            one.rollback(); one.close()
            two.rollback(); two.close()

    def test_a_claim_bumps_the_epoch(self):
        session = self.Session()
        try:
            claimed = outbox.claim(session, now=T0)
            session.commit()
            self.assertTrue(all(e.owner_epoch == 1 for e in claimed))

            # An expired lease is re-claimable, and that is a new epoch - the
            # previous holder's outcome is now stale by construction.
            for e in claimed:
                e.leased_until = at(-1)
            session.commit()
            again = outbox.claim(session, now=at(1))
            session.commit()
            self.assertTrue(again)
            self.assertTrue(all(e.owner_epoch == 2 for e in again))
        finally:
            session.close()


# ------------------------------------------------------------- the fence


class StaleOwnerIsDiscarded(PgCase):
    """The case the plan calls out by name: a late `terminal` vs a verdict.

    Upsert on `LabOS Attempt ID` prevents a duplicate *row*. It does nothing
    about a stale *last write*. Without the fence, a worker whose lease expired
    mid-send lands its outcome on top of an attempt that has since been
    reviewed.
    """

    def test_an_outcome_from_a_lost_lease_is_not_recorded(self):
        session = self.Session()
        try:
            outbox.enqueue(session, "A", outbox.TERMINAL, {"LabOS Attempt ID": "A"})
            session.commit()

            claimed = outbox.claim(session, now=T0)
            session.commit()
            entry = claimed[0]
            stale_epoch = entry.owner_epoch

            # Somebody else re-claims it: the lease expired while we were
            # sending, and the epoch moved.
            other = self.Session()
            try:
                row = other.get(outbox.SyncOutbox, entry.id)
                row.leased_until = at(-1)
                other.commit()
                taken = outbox.claim(other, now=at(1))
                other.commit()
                self.assertEqual(len(taken), 1)
                self.assertGreater(taken[0].owner_epoch, stale_epoch)
            finally:
                other.close()

            # Our epoch is stale, so we no longer own it...
            self.assertFalse(outbox.owns(session, entry, stale_epoch))
            # ...and re-asserting the lease must fail rather than steal it back.
            self.assertFalse(outbox.reassert_lease(session, entry, stale_epoch))
        finally:
            session.close()

    def test_the_worker_discards_instead_of_recording(self):
        """End to end through `run_cycle`: sent, then thrown away."""
        session = self.Session()
        try:
            outbox.enqueue(session, "A", outbox.TERMINAL, {"LabOS Attempt ID": "A"})
            session.commit()

            sent = []

            def send(entry):
                sent.append(entry.attempt_id)
                # While we are "in flight", another claimer takes the entry.
                thief = self.Session()
                try:
                    row = thief.get(outbox.SyncOutbox, entry.id)
                    row.owner_epoch = (row.owner_epoch or 0) + 5
                    thief.commit()
                finally:
                    thief.close()
                return "recSTALE"

            result = sync_worker.run_cycle(session, send, now=T0)

            self.assertEqual(sent, ["A"], "the send did happen")
            self.assertEqual(result.discarded, 1, "and its outcome was discarded")
            self.assertEqual(result.delivered, 0)

            # The entry is untouched by the loser: not done, no record id.
            row = session.get(outbox.SyncOutbox, 1)
            session.refresh(row)
            self.assertNotEqual(row.state, outbox.DONE)
            state = session.get(outbox.SyncAttemptState, "A")
            self.assertIsNone(state, "no watermark from a discarded outcome")
        finally:
            session.close()


# ----------------------------------------------------- lease vs the budget


class LeaseMatchesTheRetryBudget(unittest.TestCase):
    """No database needed: this guards a constant from drifting again."""

    def test_the_lease_outlasts_the_client_worst_case(self):
        budget = airtable_client.request_budget_seconds()
        self.assertGreater(
            outbox.DEFAULT_LEASE_SECONDS, budget,
            "a lease shorter than one send's worst case gets a working worker's "
            "entry stolen - which is how the 120s lease was wrong against a "
            "168s budget",
        )

    def test_the_old_hard_coded_lease_would_now_fail_this(self):
        """Documents the defect rather than just fixing it."""
        self.assertLess(120, airtable_client.request_budget_seconds())


if __name__ == "__main__":
    unittest.main()
