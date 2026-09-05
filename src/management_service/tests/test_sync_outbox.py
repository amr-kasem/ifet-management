"""Outbox and offline-recovery tests — synthetic fixtures, no network, no Postgres.

These cover the acceptance cases the design package calls B (offline
resilience) and C′ (per-attempt delivery ordering). They are the ones that would
let a real defect reach a customer: a lost result, a verdict delivered ahead of
the measurements behind it, or a queue that stalls the whole lab because one
record is bad.

Everything runs against SQLite in memory, so the SQL is real but the test is
offline and takes milliseconds. The Airtable side is a `Transport` object that
can be told to fail, to lose a response after succeeding, or to recover.

The rest of the suite is stdlib-only and runs anywhere. This module cannot be:
the outbox *is* database behaviour, and testing it against a fake would test the
fake. So it skips cleanly where SQLAlchemy is absent and runs in full inside the
container, in CI, or in any venv with `requirements.txt` installed.
"""

import datetime as dt
import unittest

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
except ImportError as exc:                                  # pragma: no cover
    raise unittest.SkipTest(f"sqlalchemy not installed: {exc}") from exc

from app.airtable.errors import (
    AirtableAuthError, AirtableServerError, AirtableTransportError,
)
from app.data.models import Base
from app.sync import outbox, state as sync_state, worker

T0 = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.timezone.utc)


def at(seconds):
    return T0 + dt.timedelta(seconds=seconds)


class Transport:
    """A synthetic Airtable. Scripted per call, and it remembers everything."""

    def __init__(self, *outcomes):
        # Each outcome: None -> success, an Exception -> raise it.
        self.outcomes = list(outcomes)
        self.sent = []          # (attempt_id, seq, phase)
        self.accepted = []      # what Airtable would actually hold

    def __call__(self, entry):
        self.sent.append((entry.attempt_id, entry.attempt_seq, entry.phase))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            # `lost_response` models the ambiguous timeout: Airtable accepted
            # the write, then the connection dropped before we heard back.
            if getattr(outcome, "lost_response", False):
                self.accepted.append((entry.attempt_id, entry.attempt_seq))
            raise outcome
        self.accepted.append((entry.attempt_id, entry.attempt_seq))
        return f"rec{entry.attempt_id}{entry.attempt_seq}"


def lost(exc):
    """Mark an exception as 'Airtable accepted it, we never found out'."""
    exc.lost_response = True
    return exc


class Base_(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()

    def tearDown(self):
        self.session.close()

    def enqueue(self, attempt_id, phase, updated_at=None, **payload):
        entry = outbox.enqueue(
            self.session, attempt_id, phase,
            payload or {"LabOS Attempt ID": attempt_id, "phase": phase},
            payload_updated_at=updated_at,
        )
        self.session.commit()
        return entry


# --------------------------------------------------------------- enqueueing


class Enqueue(Base_):
    def test_sequence_is_monotonic_within_an_attempt(self):
        self.enqueue("A", outbox.CREATE)
        self.enqueue("A", outbox.TERMINAL)
        self.enqueue("A", outbox.VERDICT)
        seqs = [e.attempt_seq for e in
                self.session.query(outbox.SyncOutbox)
                .order_by(outbox.SyncOutbox.attempt_seq)]
        self.assertEqual(seqs, [1, 2, 3])

    def test_sequences_are_independent_across_attempts(self):
        self.enqueue("A", outbox.CREATE)
        self.enqueue("B", outbox.CREATE)
        rows = {e.attempt_id: e.attempt_seq
                for e in self.session.query(outbox.SyncOutbox)}
        self.assertEqual(rows, {"A": 1, "B": 1})

    def test_enqueue_does_not_commit(self):
        """The caller commits, in the same transaction as the attempt row."""
        outbox.enqueue(self.session, "A", outbox.CREATE, {"x": 1})
        other = self.Session()
        self.addCleanup(other.close)
        self.assertEqual(outbox.queue_depth(other), 0)
        self.session.commit()
        self.assertEqual(outbox.queue_depth(other), 1)


# ------------------------------------------------- C′ — per-attempt ordering


class Ordering(Base_):
    def test_phases_deliver_in_sequence(self):
        for phase in (outbox.CREATE, outbox.TERMINAL, outbox.VERDICT):
            self.enqueue("A", phase)
        t = Transport()
        worker.drain(self.session, t, now=T0)
        self.assertEqual([s[2] for s in t.sent],
                         [outbox.CREATE, outbox.TERMINAL, outbox.VERDICT])

    def test_verdict_never_overtakes_a_failed_terminal(self):
        """C′1 — the record must never hold a verdict with no measurements."""
        self.enqueue("A", outbox.CREATE)
        self.enqueue("A", outbox.TERMINAL)
        self.enqueue("A", outbox.VERDICT)
        t = Transport(None, AirtableServerError("503"))
        worker.run_cycle(self.session, t, now=T0)
        worker.run_cycle(self.session, t, now=T0)
        self.assertNotIn(outbox.VERDICT, [s[2] for s in t.sent])

    def test_one_blocked_attempt_does_not_stall_the_others(self):
        """C′2 — a poisoned record must never stop the lab."""
        self.enqueue("A", outbox.CREATE)
        self.enqueue("B", outbox.CREATE)
        self.enqueue("C", outbox.CREATE)
        a = self.session.query(outbox.SyncOutbox).filter_by(attempt_id="A").one()
        a.state = outbox.PARKED
        self.session.commit()

        t = Transport()
        worker.drain(self.session, t, now=T0)
        delivered = {s[0] for s in t.sent}
        self.assertEqual(delivered, {"B", "C"})
        self.assertEqual(outbox.blocked_attempts(self.session), ["A"])

    def test_no_cross_phase_coalescing(self):
        """C′3 — collapsing terminal+verdict would drop whichever fields differ."""
        self.enqueue("A", outbox.TERMINAL, note="measurements")
        self.enqueue("A", outbox.VERDICT, note="verdict")
        t = Transport()
        worker.drain(self.session, t, now=T0)
        self.assertEqual(len(t.sent), 2)

    def test_restart_resumes_at_the_same_position(self):
        """C′6 — sequence order survives a process restart."""
        for phase in (outbox.CREATE, outbox.TERMINAL, outbox.VERDICT):
            self.enqueue("A", phase)
        t = Transport()
        worker.run_cycle(self.session, t, limit=1, now=T0)
        self.session.close()

        self.session = self.Session()      # a "new process"
        worker.drain(self.session, t, now=at(1))
        self.assertEqual([s[1] for s in t.sent], [1, 2, 3])


# ------------------------------------------------------ B — offline recovery


class OfflineRecovery(Base_):
    def test_queue_grows_while_airtable_is_unreachable(self):
        """B1 — the result is already durable; testing continues."""
        for phase in (outbox.CREATE, outbox.TERMINAL):
            self.enqueue("A", phase)
        t = Transport(AirtableTransportError("no route"),
                      AirtableTransportError("no route"))
        worker.run_cycle(self.session, t, now=T0)
        self.assertEqual(outbox.queue_depth(self.session), 2)
        self.assertEqual(sync_state.status(self.session, now=T0)["led"],
                         sync_state.LED_RED)

    def test_reconnect_drains_with_no_duplicates(self):
        """B2 — everything lands exactly once."""
        for phase in (outbox.CREATE, outbox.TERMINAL, outbox.VERDICT):
            self.enqueue("A", phase)
        offline = Transport(*[AirtableTransportError("down")] * 3)
        worker.run_cycle(self.session, offline, now=T0)

        online = Transport()
        worker.drain(self.session, online, now=at(600))
        self.assertEqual(len(online.accepted), 3)
        self.assertEqual(len(set(online.accepted)), 3)
        self.assertEqual(outbox.queue_depth(self.session), 0)

    def test_status_is_accurate_with_the_worker_dead(self):
        """B3 — report-api answers, and the depth is still true."""
        self.enqueue("A", outbox.CREATE)
        self.enqueue("B", outbox.CREATE)
        sync_state.heartbeat(self.session, now=T0)
        self.session.commit()

        st = sync_state.status(self.session, now=at(10_000))
        self.assertFalse(st["worker_alive"])
        self.assertEqual(st["led"], sync_state.LED_RED)
        self.assertEqual(st["queue_depth"], 2)      # counted live, not cached

    def test_crash_mid_push_is_retried_not_lost(self):
        """B4 — an expired lease returns the entry to its queue."""
        self.enqueue("A", outbox.CREATE)
        claimed = outbox.claim(self.session, now=T0)
        self.session.commit()
        self.assertEqual(claimed[0].state, outbox.INFLIGHT)

        # ...worker dies here, having neither completed nor failed the entry.
        self.assertEqual(outbox.claim(self.session, now=at(30)), [])
        again = outbox.claim(self.session, now=at(3600))
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].attempt_seq, 1)

    def test_backoff_delays_the_retry(self):
        self.enqueue("A", outbox.CREATE)
        t = Transport(AirtableServerError("503"))
        worker.run_cycle(self.session, t, now=T0)
        self.assertEqual(outbox.claim(self.session, now=T0), [])
        self.assertEqual(len(outbox.claim(self.session, now=at(600))), 1)


# ------------------------------------------------------- parking, never losing


class Parking(Base_):
    def test_entry_is_parked_not_dropped(self):
        """C′5 — silent discard is the one failure mode that loses a result."""
        self.enqueue("A", outbox.CREATE)
        t = Transport(*[AirtableServerError("503")] * 20)
        for i in range(10):
            worker.run_cycle(self.session, t, now=at(i * 10_000), max_attempts=3)

        entry = self.session.query(outbox.SyncOutbox).one()
        self.assertEqual(entry.state, outbox.PARKED)
        self.assertIsNotNone(entry.payload)          # payload survives
        self.assertIn("503", entry.last_error)       # so does the reason
        self.assertEqual(outbox.queue_depth(self.session), 1)

    def test_parked_entry_can_be_resumed(self):
        self.enqueue("A", outbox.CREATE)
        entry = self.session.query(outbox.SyncOutbox).one()
        entry.state = outbox.PARKED
        self.session.commit()

        outbox.resume(self.session, entry.id, now=T0)
        self.session.commit()
        t = Transport()
        worker.drain(self.session, t, now=T0)
        self.assertEqual(len(t.accepted), 1)

    def test_auth_failure_parks_immediately(self):
        """Retrying a 401 eight times only delays the operator finding out."""
        self.enqueue("A", outbox.CREATE)
        t = Transport(AirtableAuthError("401"))
        worker.run_cycle(self.session, t, now=T0)
        self.assertEqual(self.session.query(outbox.SyncOutbox).one().state,
                         outbox.PARKED)


# --------------------------------------------- the ambiguous-timeout duplicate


class MonotonicGuard(Base_):
    def test_duplicate_terminal_cannot_reset_a_delivered_verdict(self):
        """C5 — the case that would silently un-review a reviewed record."""
        self.enqueue("A", outbox.TERMINAL, updated_at=at(0))
        self.enqueue("A", outbox.VERDICT, updated_at=at(60))
        t = Transport()
        worker.drain(self.session, t, now=T0)

        # A `terminal` we recorded as failed had in fact been accepted, and is
        # replayed after the verdict landed.
        replay = outbox.enqueue(self.session, "A", outbox.TERMINAL,
                                {"stale": True}, payload_updated_at=at(0))
        self.session.commit()

        after = Transport()
        worker.drain(self.session, after, now=at(120))
        self.assertEqual(after.sent, [])                  # never went out
        self.assertEqual(replay.state, outbox.DONE)       # closed as superseded

    def test_watermark_survives_the_queue_emptying(self):
        self.enqueue("A", outbox.CREATE, updated_at=at(0))
        worker.drain(self.session, Transport(), now=T0)
        self.assertEqual(outbox.queue_depth(self.session), 0)

        state = self.session.get(outbox.SyncAttemptState, "A")
        self.assertEqual(state.delivered_seq, 1)
        self.assertIsNotNone(state.airtable_record_id)

    def test_lost_response_then_retry_delivers_once_in_airtable(self):
        """The upsert on LabOS Attempt ID is what makes the retry safe."""
        self.enqueue("A", outbox.CREATE, updated_at=at(0))
        t = Transport(lost(AirtableTransportError("connection reset")))
        worker.run_cycle(self.session, t, now=T0)
        worker.drain(self.session, t, now=at(600))

        # Sent twice, but both writes target the same merge key, so Airtable
        # holds one record - and our watermark now agrees.
        self.assertEqual(len(t.sent), 2)
        self.assertEqual({a[0] for a in t.accepted}, {"A"})
        self.assertEqual(outbox.queue_depth(self.session), 0)


# ------------------------------------------------------------------ the LED


class Led(Base_):
    def _beat(self, now):
        sync_state.heartbeat(self.session, now=now)
        self.session.commit()

    def test_green_when_healthy_and_empty(self):
        self._beat(T0)
        self.assertEqual(sync_state.status(self.session, now=T0)["led"],
                         sync_state.LED_GREEN)

    def test_amber_while_work_is_queued(self):
        self.enqueue("A", outbox.CREATE)
        self._beat(T0)
        self.assertEqual(sync_state.status(self.session, now=T0)["led"],
                         sync_state.LED_AMBER)

    def test_red_when_an_attempt_is_blocked(self):
        self.enqueue("A", outbox.CREATE)
        self.session.query(outbox.SyncOutbox).one().state = outbox.PARKED
        self._beat(T0)
        st = sync_state.status(self.session, now=T0)
        self.assertEqual(st["led"], sync_state.LED_RED)
        self.assertEqual(st["blocked_attempts"], ["A"])

    def test_status_works_before_the_worker_has_ever_run(self):
        st = sync_state.status(self.session, now=T0)
        self.assertEqual(st["led"], sync_state.LED_RED)
        self.assertFalse(st["worker_alive"])
        self.assertEqual(st["queue_depth"], 0)


if __name__ == "__main__":
    unittest.main()
