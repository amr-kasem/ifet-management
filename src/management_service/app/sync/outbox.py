"""The transactional outbox — design package §5.1, §5.2.2.

The guarantee this module exists to provide, in one sentence: **an Airtable
outage can never lose a test result, and can never stop a test.**

That is achieved structurally rather than by careful coding. `enqueue()` takes a
session and adds a row; the caller commits it **in the same transaction as the
attempt itself**. Either both land or neither does. From that moment the result
is durable in Postgres and Airtable's availability is somebody else's problem —
the worker drains the queue whenever the network returns.

Two properties do the real work, and both are easy to get wrong:

**Ordering is per attempt, not global.** The phases of one attempt are not
independent: if `terminal` fails and `verdict` succeeds, Airtable holds a
verdict with no measurements behind it — a record that looks complete and is
not. So each attempt is its own FIFO. A failure head-of-line blocks *that
attempt only*; every other attempt keeps flowing, because one poisoned record
must never stall the lab.

**Nothing is ever silently dropped.** After `max_attempts` an entry is *parked*,
not discarded: the attempt's queue stops, `/sync/status` shows it, and an
operator resumes it. Silent discard is the one failure mode that would lose a
result while reporting success.

Storage-agnostic in spirit but SQLAlchemy in fact, so the tests run the real SQL
against SQLite in memory and the production path against Postgres unchanged.
"""

import datetime as dt

from sqlalchemy import (
    Column, DateTime, Integer, JSON, String, Text, UniqueConstraint, case, func,
)
from sqlalchemy.exc import IntegrityError

# NOT from `..airtable.client`. This module is persistence only — it must be
# importable by `report-api` without dragging the HTTP transport into the
# request path, which is what makes the transactional outbox a real seam
# rather than a naming convention.
from ..retry_budget import request_budget_seconds
from ..data.models import Base

# --- entry states ----------------------------------------------------------
PENDING = "pending"     # waiting its turn, or waiting out a backoff
INFLIGHT = "inflight"   # leased by a worker
DONE = "done"           # delivered, or superseded by a newer payload
PARKED = "parked"       # gave up after max_attempts; needs a human

OPEN_STATES = (PENDING, INFLIGHT, PARKED)

# --- phases, in delivery order (design package §5.2) -----------------------
CREATE = "create"
TERMINAL = "terminal"
VERDICT = "verdict"
ATTACHMENT = "attachment"

DEFAULT_MAX_ATTEMPTS = 8

# How many times `enqueue` will retry a colliding sequence allocation before
# giving up. Each retry is one lost race against a concurrent enqueue for the
# same attempt; more than a couple means something else is wrong.
ENQUEUE_MAX_RETRIES = 5

# How long a worker may hold an entry before another worker may take it.
#
# **This is one decision with the client's retry budget, not two.** A lease
# shorter than the worst case a single send can legitimately take means a
# still-working worker gets its entry stolen: two workers then send the same
# phase, and while the upsert prevents a duplicate *row*, the loser can still
# record a stale outcome over the winner's. That is what `owner_epoch` fences,
# but the lease should not create the race in the first place.
#
# So it is derived, with margin, from `client.request_budget_seconds()`. The
# previous hard-coded 120 was **shorter than the client's own worst case of
# ~168 s**, which is exactly the bug.
LEASE_SAFETY_FACTOR = 1.5
DEFAULT_LEASE_SECONDS = int(request_budget_seconds() * LEASE_SAFETY_FACTOR) + 1


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _aware(value):
    """Postgres hands back tz-aware datetimes; SQLite hands back naive ones."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


class SyncOutbox(Base):
    """One pending write to Airtable, for one phase of one attempt."""

    __tablename__ = "sync_outbox"
    __table_args__ = (
        # The FIFO invariant, enforced by the database rather than by the
        # worker: an attempt cannot have two entries claiming the same position.
        UniqueConstraint("attempt_id", "attempt_seq", name="uq_outbox_attempt_seq"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # `labos_attempt_id` — the FIFO key, and the merge key Airtable upserts on.
    attempt_id = Column(String, nullable=False, index=True)
    # Monotonic within one attempt. Assigned under the caller's transaction, so
    # two concurrent enqueues collide on the unique constraint rather than
    # silently swapping order.
    attempt_seq = Column(Integer, nullable=False)
    phase = Column(String, nullable=False)

    payload = Column(JSON, nullable=False)
    # `labos_updated_at` from the payload. The monotonic guard reads this to
    # recognise a duplicate delivery of something we believed had failed.
    payload_updated_at = Column(DateTime(timezone=True), nullable=True)

    state = Column(String, nullable=False, default=PENDING, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    leased_until = Column(DateTime(timezone=True), nullable=True)
    # Fencing token. Bumped on every claim, so a worker holds the epoch it was
    # given and any outcome carrying an older epoch is **discarded, not
    # recorded**. Without it, a worker whose lease expired mid-send can land a
    # stale `terminal` on top of an already-reviewed verdict: the upsert stops a
    # duplicate row, never a stale last write.
    owner_epoch = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    def __repr__(self):
        return (f"SyncOutbox({self.attempt_id}#{self.attempt_seq} "
                f"{self.phase} {self.state})")


class SyncAttemptState(Base):
    """Per-attempt delivery watermark.

    Separate from the outbox because it must outlive the entries: once every
    entry for an attempt is `done` we still need to know what Airtable holds, so
    a late duplicate can be recognised as superseded rather than replayed.
    """

    __tablename__ = "sync_attempt_state"

    attempt_id = Column(String, primary_key=True)
    delivered_seq = Column(Integer, nullable=False, default=0)
    delivered_updated_at = Column(DateTime(timezone=True), nullable=True)
    airtable_record_id = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class Superseded(Exception):
    """Raised internally when an entry is older than what Airtable already has."""


def enqueue(session, attempt_id, phase, payload, payload_updated_at=None,
            _max_retries=ENQUEUE_MAX_RETRIES, _after_read=None):
    """Add one phase write to `attempt_id`'s queue.

    **Does not commit.** That is the whole point: the caller commits this in the
    same transaction as the attempt row, so a result and its intent to sync are
    atomic. A caller that commits them separately has reintroduced the failure
    mode this module exists to remove.

    Sequence allocation is `max(seq)+1` **inside a savepoint, retried on
    collision**. Read-then-insert without the savepoint is the deviation this
    replaces: two concurrent enqueues for one attempt computed the same number,
    and the loser's unique-constraint violation aborted the caller's whole
    transaction - failing the operator's save to protect a queue whose entire
    purpose is that the save never fails.
    """
    # The savepoint retry is a Postgres mechanism, and only Postgres gets it.
    #
    # On SQLite it is not merely unnecessary - it is *harmful*. pysqlite does not
    # open a transaction the way SQLAlchemy's SAVEPOINT support needs, so
    # `begin_nested()` + `flush()` commits the INSERT then and there. The
    # enqueue would survive the caller rolling back, which breaks the one
    # guarantee this module exists to provide: the entry and the attempt row
    # land together or not at all. Verified 2026-09-06 - the row outlived a
    # `session.rollback()`.
    #
    # Nothing is lost by skipping it. SQLite has a single writer, so there is no
    # concurrent enqueue to collide with, and `max(seq)+1` is safe: the pending
    # object is flushed by the next query's autoflush, so a second enqueue in
    # the same session sees the first.
    if session.bind is None or session.bind.dialect.name != "postgresql":
        next_seq = (session.query(func.coalesce(func.max(SyncOutbox.attempt_seq), 0))
                    .filter(SyncOutbox.attempt_id == attempt_id)
                    .scalar()) + 1
        entry = SyncOutbox(
            attempt_id=attempt_id,
            attempt_seq=next_seq,
            phase=phase,
            payload=payload,
            payload_updated_at=payload_updated_at,
            state=PENDING,
            attempts=0,
            owner_epoch=0,
            next_attempt_at=None,
        )
        if _after_read is not None:
            _after_read(next_seq)
        session.add(entry)
        return entry

    last_error = None
    for _ in range(_max_retries):
        next_seq = (session.query(func.coalesce(func.max(SyncOutbox.attempt_seq), 0))
                    .filter(SyncOutbox.attempt_id == attempt_id)
                    .scalar()) + 1
        entry = SyncOutbox(
            attempt_id=attempt_id,
            attempt_seq=next_seq,
            phase=phase,
            payload=payload,
            payload_updated_at=payload_updated_at,
            state=PENDING,
            attempts=0,
            owner_epoch=0,
            # NULL, not now(): a fresh entry has no backoff to wait out, and
            # stamping it with wall-clock would make eligibility depend on clock
            # skew between whoever enqueued and whoever claims.
            next_attempt_at=None,
        )
        if _after_read is not None:
            # Test seam. The collision this retry loop exists for needs both
            # callers to have read `max(seq)` before either inserts, and hoping
            # for that timing window makes a flaky test rather than a proof. The
            # concurrency suite passes a barrier here. Production passes None.
            _after_read(next_seq)

        try:
            # A SAVEPOINT, so that losing the race rolls back **this insert
            # only** and leaves the caller's transaction - the domain save -
            # untouched and committable. Without it, the unique-constraint
            # violation aborts the whole transaction and the operator's save
            # fails because two phases of one attempt were enqueued at once.
            # That would invert the reason this module exists.
            with session.begin_nested():
                session.add(entry)
                session.flush()
        except IntegrityError as exc:
            last_error = exc
            if entry in session:
                session.expunge(entry)
            continue
        return entry

    raise RuntimeError(
        f"could not allocate an outbox sequence for attempt {attempt_id} after "
        f"{_max_retries} attempts"
    ) from last_error


def _channel(column=None):
    """Which of an attempt's two queues an entry belongs to.

    `0` = the record channel (`create` → `terminal` → `verdict`), strictly
    ordered because each phase merges onto the row the previous one made.
    `1` = the attachment channel.

    **Contract §4 says "plus separately tracked attachment delivery" and this is
    what makes that true.** Grouping heads by `attempt_id` alone put attachments
    in the same FIFO as the record phases, so an attachment enqueued before the
    verdict became the head — and a parked attachment then made the whole
    attempt undeliverable, verdict included. §6's reason for a separate channel
    is precisely that attachments "may finish after terminal state without
    changing measured evidence": a queued file must not hold up a measured
    result, and a verdict is a measured result.

    Two channels, not a bypass of FIFO: ordering is still strict *within* each.
    """
    col = SyncOutbox.phase if column is None else column
    return case((col == ATTACHMENT, 1), else_=0)


def _head_ids(session):
    """The lowest open `attempt_seq` per attempt **per channel**.

    Two candidates per attempt at most: the next record phase, and the next
    attachment. A plain `min()` group-by rather than a window function, so the
    same SQL runs on SQLite in the tests and Postgres in production.
    """
    channel = _channel().label("chan")
    return (session.query(func.min(SyncOutbox.attempt_seq).label("seq"),
                          SyncOutbox.attempt_id.label("aid"),
                          channel)
            .filter(SyncOutbox.state.in_(OPEN_STATES))
            .group_by(SyncOutbox.attempt_id, _channel())
            .subquery())


def claim(session, limit=10, now=None, lease_seconds=DEFAULT_LEASE_SECONDS):
    """Lease up to `limit` deliverable entries, at most one per attempt.

    Deliverable means: this entry is its attempt's head, it is `pending` (or an
    `inflight` lease that expired — a worker that crashed mid-push), and its
    backoff has elapsed. A `parked` head makes the whole attempt undeliverable,
    which is the head-of-line block, scoped to that attempt alone.
    """
    now = now or _now()
    heads = _head_ids(session)
    query = (session.query(SyncOutbox)
             .join(heads, (SyncOutbox.attempt_id == heads.c.aid)
                   & (SyncOutbox.attempt_seq == heads.c.seq)
                   # ...and the same channel, so the record head and the
                   # attachment head are both claimable rather than the lower
                   # sequence hiding the other.
                   & (_channel() == heads.c.chan))
             .order_by(SyncOutbox.attempt_id, SyncOutbox.attempt_seq))

    # `SELECT ... FOR UPDATE OF sync_outbox SKIP LOCKED` - exclusive ownership
    # enforced by the database, not by deploying exactly one worker. SKIP LOCKED
    # rather than NOWAIT so a second worker takes different work instead of
    # erroring, and `of=` so only the outbox rows are locked and not the
    # head-computing subquery.
    #
    # SQLite ignores the locking clause, which is precisely why these guarantees
    # are only meaningful under the Postgres harness in
    # `tests/postgres_harness/`.
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(of=SyncOutbox, skip_locked=True)

    rows = query.all()

    claimed = []
    for row in rows:
        if len(claimed) >= limit:
            break
        if row.state == PARKED:
            continue
        if row.state == INFLIGHT and _aware(row.leased_until) and _aware(row.leased_until) > now:
            continue                      # still held by a live worker
        if _aware(row.next_attempt_at) and _aware(row.next_attempt_at) > now:
            continue                      # backing off
        row.state = INFLIGHT
        row.leased_until = now + dt.timedelta(seconds=lease_seconds)
        # Every claim is a new epoch, including a claim of an expired lease. The
        # previous holder's epoch is now stale and its outcome will be discarded.
        row.owner_epoch = (row.owner_epoch or 0) + 1
        row.updated_at = now
        claimed.append(row)
    return claimed


def owns(session, entry, epoch):
    """Does `epoch` still hold this entry?

    Read straight from the database rather than from the ORM object, because the
    object is what the caller has been holding across a slow network send and is
    exactly what may be stale.
    """
    row = (session.query(SyncOutbox.owner_epoch)
           .filter(SyncOutbox.id == entry.id).one_or_none())
    return row is not None and row[0] == epoch


def reassert_lease(session, entry, epoch, now=None, lease_seconds=DEFAULT_LEASE_SECONDS):
    """Extend the lease immediately before a send. False means we lost it.

    Called **per send, not per batch**. A batch-stamped lease is a lease that
    expires while the ninth entry is still waiting behind eight slow sends, so
    another worker takes it while this one is about to send it too.
    """
    now = now or _now()
    if not owns(session, entry, epoch):
        return False
    entry.leased_until = now + dt.timedelta(seconds=lease_seconds)
    entry.updated_at = now
    return True


def is_superseded(session, entry):
    """True if Airtable already holds something newer for this attempt.

    Ordering stops *us* sending out of sequence. This catches the other case: a
    delivery we recorded as failed that actually succeeded, replayed after a
    later phase has already landed. Without it, a duplicate `terminal` can reset
    a reviewed record to `Pending`.
    """
    state = session.get(SyncAttemptState, entry.attempt_id)
    if state is None:
        return False
    if entry.attempt_seq <= (state.delivered_seq or 0):
        return True
    watermark = _aware(state.delivered_updated_at)
    stamp = _aware(entry.payload_updated_at)
    return bool(watermark and stamp and stamp < watermark)


def mark_done(session, entry, airtable_record_id=None, now=None):
    """Record a successful delivery and advance the attempt's watermark."""
    now = now or _now()
    entry.state = DONE
    entry.leased_until = None
    entry.last_error = None
    entry.updated_at = now

    state = session.get(SyncAttemptState, entry.attempt_id)
    if state is None:
        state = SyncAttemptState(attempt_id=entry.attempt_id, delivered_seq=0)
        session.add(state)
    if entry.attempt_seq > (state.delivered_seq or 0):
        state.delivered_seq = entry.attempt_seq
        state.delivered_updated_at = entry.payload_updated_at
    if airtable_record_id:
        state.airtable_record_id = airtable_record_id
    state.updated_at = now
    return state


def backoff_seconds(attempts):
    """Exponential, capped. Deterministic so the tests can assert on it."""
    return min(2 ** max(attempts - 1, 0), 300)


def mark_failed(session, entry, error, now=None, max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Record a failure, then either schedule a retry or park the entry.

    Parking stops this attempt's queue and nothing else. The entry stays in the
    table with its payload and its last error intact, because the operator who
    resumes it needs to see both.
    """
    now = now or _now()
    entry.attempts = (entry.attempts or 0) + 1
    entry.last_error = str(error)[:2000]
    entry.leased_until = None
    entry.updated_at = now
    if entry.attempts >= max_attempts:
        entry.state = PARKED
        entry.next_attempt_at = None
    else:
        entry.state = PENDING
        entry.next_attempt_at = now + dt.timedelta(seconds=backoff_seconds(entry.attempts))
    return entry


def resume(session, entry_id, now=None):
    """Un-park an entry. The manual half of `POST /sync/queue/{id}/retry`."""
    now = now or _now()
    entry = session.get(SyncOutbox, entry_id)
    if entry is None or entry.state != PARKED:
        return None
    entry.state = PENDING
    entry.attempts = 0
    entry.next_attempt_at = now
    entry.updated_at = now
    return entry


def queue_depth(session):
    """Counted live, never cached.

    A depth reported by the worker is exactly the number that goes stale when
    the worker dies — which is the moment the number matters most.
    """
    return (session.query(func.count(SyncOutbox.id))
            .filter(SyncOutbox.state.in_(OPEN_STATES)).scalar()) or 0


def attachment_backlog(session):
    """Open entries on the attachment channel.

    Tracked separately because §6 runs attachments as their own channel: an
    attempt can be fully delivered while its photographs are still queued, and
    a status that said "Synced" then would be lying about the evidence.
    """
    return (session.query(func.count(SyncOutbox.id))
            .filter(SyncOutbox.phase == ATTACHMENT,
                    SyncOutbox.state.in_(OPEN_STATES)).scalar()) or 0


def parked_count(session):
    return (session.query(func.count(SyncOutbox.id))
            .filter(SyncOutbox.state == PARKED).scalar()) or 0


def blocked_attempts(session):
    """Attempt ids whose head is parked — the queues that have stopped."""
    heads = _head_ids(session)
    rows = (session.query(SyncOutbox.attempt_id)
            .join(heads, (SyncOutbox.attempt_id == heads.c.aid)
                  & (SyncOutbox.attempt_seq == heads.c.seq))
            .filter(SyncOutbox.state == PARKED).all())
    return [r[0] for r in rows]
