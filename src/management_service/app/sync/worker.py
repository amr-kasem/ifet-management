"""The pusher — drains the outbox, in order, without ever losing an entry.

One cycle is: claim the deliverable heads, send each, record the outcome. The
worker owns no state of its own; everything it knows is in `sync_outbox` and
`sync_state`, which is what lets it be killed at any instant and resume
correctly.

Deliberately **transport-injected**. The tests drive it with a synthetic
transport that can fail, time out ambiguously, or succeed on the third try —
none of which is reachable through a real client without a network. The
production wiring passes `app.airtable.client.AirtableClient.upsert_records`.

The error taxonomy is the client's, not a new one: `AirtableAuthError` and
`AirtableValidationError` are terminal and park immediately, because retrying a
401 or a 422 eight times just delays the operator finding out.
"""

import datetime as dt

from ..airtable.errors import (
    AirtableAuthError,
    AirtableError,
    AirtableValidationError,
    AirtableWriteForbidden,
)
from . import outbox, state as sync_state

# Failures that will never succeed on retry. Park at once and surface them.
TERMINAL_ERRORS = (AirtableAuthError, AirtableValidationError, AirtableWriteForbidden)


def _now():
    return dt.datetime.now(dt.timezone.utc)


class Result:
    """What one cycle did. Returned rather than logged so tests can assert."""

    __slots__ = ("delivered", "superseded", "failed", "parked", "claimed",
                 "discarded")

    def __init__(self):
        self.delivered = 0
        self.superseded = 0
        self.failed = 0
        self.parked = 0
        self.claimed = 0
        # Outcomes thrown away because the lease was lost mid-send. Counted
        # rather than logged, because "we sent and then discarded the result"
        # is a thing an operator may need to see.
        self.discarded = 0

    def __repr__(self):
        return (f"Result(claimed={self.claimed} delivered={self.delivered} "
                f"superseded={self.superseded} failed={self.failed} "
                f"parked={self.parked} discarded={self.discarded})")


def run_cycle(session, send, *, limit=10, now=None,
              max_attempts=outbox.DEFAULT_MAX_ATTEMPTS, commit=True):
    """Claim, deliver, record. Returns a `Result`.

    `send(entry) -> record_id | None` does the actual write. It may raise; every
    exception is caught and turned into a queue outcome, because an exception
    escaping this loop would leave entries leased and stall their attempts until
    the lease expired.
    """
    now = now or _now()
    result = Result()

    claimed = outbox.claim(session, limit=limit, now=now)
    result.claimed = len(claimed)
    # Commit the leases before sending. If the process dies mid-send, the entry
    # is visibly `inflight` with a lease that expires, rather than looking
    # `pending` while a request may still be in flight.
    if commit:
        session.commit()

    for entry in claimed:
        # The epoch this worker was granted. Every outcome below is only
        # recorded if the row still carries it.
        epoch = entry.owner_epoch

        if outbox.is_superseded(session, entry):
            # Not an error: a duplicate of something Airtable already has, or
            # older than what it holds. Closing it is the correct outcome.
            outbox.mark_done(session, entry, now=now)
            result.superseded += 1
            continue

        # Re-assert immediately before *this* send, not once for the batch: by
        # the time a slow batch reaches its last entry, a batch-stamped lease
        # may already have expired and been taken by another worker.
        if not outbox.reassert_lease(session, entry, epoch, now=now):
            result.discarded += 1
            continue
        if commit:
            session.commit()

        try:
            record_id = send(entry)
        except TERMINAL_ERRORS as exc:
            if not outbox.owns(session, entry, epoch):
                result.discarded += 1
                continue
            outbox.mark_failed(session, entry, exc, now=now,
                               max_attempts=1)   # park on the first occurrence
            result.failed += 1
            result.parked += 1
            _record_push_error(session, exc, now)
        except (AirtableError, Exception) as exc:      # noqa: B014 - breadth is deliberate
            if not outbox.owns(session, entry, epoch):
                result.discarded += 1
                continue
            outbox.mark_failed(session, entry, exc, now=now,
                               max_attempts=max_attempts)
            result.failed += 1
            if entry.state == outbox.PARKED:
                result.parked += 1
            _record_push_error(session, exc, now)
        else:
            # The fence, on the success path too - and this is the case that
            # matters most. A worker whose lease expired mid-send, whose entry
            # was re-claimed and whose attempt has since been reviewed, must not
            # write its stale outcome over the verdict. Upsert prevents a
            # duplicate row; it does nothing about a stale last write.
            if not outbox.owns(session, entry, epoch):
                result.discarded += 1
                continue
            outbox.mark_done(session, entry, airtable_record_id=record_id, now=now)
            result.delivered += 1
            st = sync_state.get_or_create(session)
            st.last_push_ok_at = now
            st.last_push_error = None
            st.last_push_error_at = None

    sync_state.heartbeat(session, now=now)
    if commit:
        session.commit()
    return result


def _record_push_error(session, exc, now):
    st = sync_state.get_or_create(session)
    st.last_push_error = str(exc)[:2000]
    st.last_push_error_at = now


def drain(session, send, *, max_cycles=100, **kwargs):
    """Run cycles until nothing more is deliverable.

    Used on reconnect and in the tests. Bounded, because a queue that will not
    drain is a condition to surface, not to spin on.
    """
    total = Result()
    for _ in range(max_cycles):
        cycle = run_cycle(session, send, **kwargs)
        total.claimed += cycle.claimed
        total.delivered += cycle.delivered
        total.superseded += cycle.superseded
        total.failed += cycle.failed
        total.parked += cycle.parked
        total.discarded += cycle.discarded
        if cycle.claimed == 0:
            break
    return total
