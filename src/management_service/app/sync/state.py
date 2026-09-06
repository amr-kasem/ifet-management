"""`sync_state` and the LED — design package §5.1.

Two decisions are baked in here, and both are about what happens when the sync
worker is *dead* rather than when it is healthy.

**report-api serves the status, not the sync service.** So a stopped sync
container still produces a useful answer instead of a connection error, and the
1 Hz browser poll never leaves the machine.

**Queue depth is counted at read time.** The worker reports its heartbeat and
nothing else. Anything the worker caches is, by construction, wrong in exactly
the situation the operator is looking at the light to understand.
"""

import datetime as dt

from sqlalchemy import Column, DateTime, Integer, String, Text

from ..data.models import Base
from . import outbox

# LED states, design package §5.1. Retained because the deployed browser bundle
# reads `led`; removing it would break the existing UI.
LED_GREEN = "green"      # healthy, nothing waiting
LED_AMBER = "amber"      # work queued - testing continues normally
LED_RED = "red"          # worker stale, or last cycle failed
# The spinner is not produced here: it is what the browser shows when report-api
# itself does not answer, which is the only real "cannot fetch".

# The contractual vocabulary - write-contract §7. These four words are what the
# Airtable team asked for and what the operator's status chip must show, so they
# are served alongside `led` rather than instead of it. `led` is a colour for a
# lamp; these are the states we owe an answer in.
SYNC_SYNCED = "Synced"                  # nothing open, nothing failed
SYNC_PENDING = "Pending"                # queued; testing continues normally
SYNC_FAILED = "Sync Failed"             # a cycle errored, or the worker is gone
SYNC_RETRY_REQUIRED = "Retry Required"  # parked - it needs a human, not time

DEFAULT_HEARTBEAT_TIMEOUT = 180


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


class SyncState(Base):
    """Singleton row: what the worker last managed to do."""

    __tablename__ = "sync_state"

    id = Column(Integer, primary_key=True, default=1)
    worker_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    last_push_ok_at = Column(DateTime(timezone=True), nullable=True)
    last_push_error = Column(Text, nullable=True)
    last_push_error_at = Column(DateTime(timezone=True), nullable=True)
    last_pull_ok_at = Column(DateTime(timezone=True), nullable=True)
    last_pull_error = Column(Text, nullable=True)
    last_pull_error_at = Column(DateTime(timezone=True), nullable=True)
    # Bumped only when a pull actually changed the mirror, so the UI refetches
    # on real change rather than on every cycle.
    revision = Column(Integer, nullable=False, default=0)
    enabled = Column(String, nullable=True)


def get_or_create(session):
    state = session.get(SyncState, 1)
    if state is None:
        state = SyncState(id=1, revision=0)
        session.add(state)
    return state


def heartbeat(session, now=None):
    state = get_or_create(session)
    state.worker_heartbeat_at = now or _now()
    return state


def status(session, now=None, heartbeat_timeout=DEFAULT_HEARTBEAT_TIMEOUT):
    """The payload behind `GET /sync/status`. Never raises, never calls Airtable."""
    now = now or _now()
    state = session.get(SyncState, 1)
    depth = outbox.queue_depth(session)
    parked = outbox.parked_count(session)
    blocked = outbox.blocked_attempts(session)

    beat = _aware(state.worker_heartbeat_at) if state else None
    beat_age = (now - beat).total_seconds() if beat else None
    worker_alive = beat_age is not None and beat_age <= heartbeat_timeout

    push_err = state.last_push_error if state else None
    pull_err = state.last_pull_error if state else None

    attachments = outbox.attachment_backlog(session)

    if not worker_alive or parked or push_err or pull_err:
        led = LED_RED
    elif depth:
        led = LED_AMBER
    else:
        led = LED_GREEN

    # Ordered by what the operator must do about it, most actionable first.
    # `parked` outranks a transient error because it will never clear on its
    # own, and both outrank a queue that is merely draining.
    if parked:
        sync_status = SYNC_RETRY_REQUIRED
    elif not worker_alive or push_err or pull_err:
        sync_status = SYNC_FAILED
    elif depth or attachments:
        sync_status = SYNC_PENDING
    else:
        sync_status = SYNC_SYNCED

    return {
        "led": led,
        "status": sync_status,
        "attachment_backlog": attachments,
        "worker_alive": worker_alive,
        "heartbeat_age_seconds": beat_age,
        "queue_depth": depth,
        "parked": parked,
        "blocked_attempts": blocked,
        "revision": state.revision if state else 0,
        "last_push_ok_at": _aware(state.last_push_ok_at).isoformat() if state and state.last_push_ok_at else None,
        "last_pull_ok_at": _aware(state.last_pull_ok_at).isoformat() if state and state.last_pull_ok_at else None,
        "last_push_error": push_err,
        "last_pull_error": pull_err,
    }
