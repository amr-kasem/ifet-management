"""Turn a saved attempt into queued outbox entries. The wiring, and nothing else.

**This module is the seam the whole design rests on.** Everything above it is a
domain save; everything below it is a worker in another container. It is called
from inside the request's transaction and it never opens a socket — which is why
it lives beside the outbox and imports only payload construction.

Four call sites, one per phase (delivery plan §4.7):

    POST .../trials                     -> create
    PUT  /test-results/{id}/finish      -> terminal
    PUT  /test-results/{id}/verdict     -> verdict
    POST /test-results/{id}/photos      -> attachment   (one entry per photograph)
    POST /shots/{id}/photos             -> attachment

**Does not commit.** The caller commits, so the result row and its intent to
sync land together or not at all (contract §4). A caller that commits them
separately has reintroduced the failure mode the outbox exists to remove.

**Nothing here may raise into the request.** The operator's save is the thing
being protected: a result that has physically happened must be recorded even if
we cannot describe it to Airtable yet. So the two failure modes are both
absorbed and made visible instead:

* **No Airtable linkage** — the job was created locally and has no `rec…` ids.
  That is a first-class mode, not a degraded one (§4.6), so the attempt is
  marked `Excluded` and nothing is queued. It will never be published, and that
  is correct.
* **A payload the envelope refuses** — a real defect on our side, in a linked
  attempt. Queuing it would put a known-bad payload in front of the worker;
  dropping it silently would lose the fact. So the reason is recorded on the
  attempt and surfaces in `GET /sync/status` as a sync failure, where a human
  sees it.
"""

import logging

from ..airtable import envelope
from ..airtable.envelope import EnvelopeError
from ..airtable.mapping import envelope_values, is_syncable
from ..data.attempts import EXCLUDED
from . import outbox

log = logging.getLogger("app.sync.publish")

CREATE, TERMINAL, VERDICT, ATTACHMENT = (
    outbox.CREATE, outbox.TERMINAL, outbox.VERDICT, outbox.ATTACHMENT)

_BUILDERS = {
    CREATE: envelope.build_start,
    TERMINAL: envelope.build_terminal,
    VERDICT: envelope.build_verdict,
}


def concrete(session, attempt):
    """The attempt re-read as its own subclass, or the row as given.

    **Why this is necessary and not defensive.** `TestResult` declares no
    polymorphic discriminator, so `session.query(TestResult).filter(...)`
    returns a *base* instance whatever subclass row exists beside it — it has no
    `static_test` / `manual_test` / `missile_impact_test` attribute at all. The
    `finish`, `verdict` and photo routes all fetch that way, so `owning_test()`
    saw None and the attempt was excluded as though it had no Airtable origin.
    Silently: "no parent" and "no Airtable linkage" produce the same answer, and
    the second is a legitimate state.

    `app/main.py:_impact_attempt` already works around the same thing for shots.
    This is that pattern, generalised, in the one place the payload needs it.
    """
    from ..data.models import (CyclicTestResult, ImpactTestResult,
                               ManualTestResult, StaticTestResult)
    if any(hasattr(attempt, a) for a in
           ("static_test", "cyclic_test", "manual_test", "missile_impact_test")):
        return attempt
    for cls in (StaticTestResult, CyclicTestResult, ManualTestResult,
                ImpactTestResult):
        found = session.query(cls).filter(cls.id == attempt.id).first()
        if found is None:
            continue
        if found is not attempt:
            # **Refresh, or the payload is built from a stale row.** Without a
            # polymorphic discriminator the identity map holds the base instance
            # and the subclass instance as two separate objects for one row, so
            # `attempt.status = "Completed"` on the base leaves the subclass
            # object still reading "In Progress". The finish route already loads
            # an `ImpactTestResult` for its shot checks, so on the impact path
            # that second object exists before we get here — and the terminal
            # payload was refused as non-terminal for a run that had completed.
            #
            # Safe because every call site flushes first: the row in the
            # database is the row we mean, and this re-reads exactly that.
            session.refresh(found)
        return found
    return attempt


def record_phase(session, attempt, phase):
    """Queue one record-channel phase for `attempt`. Returns the entry or None.

    None means "deliberately not queued" — excluded, already terminal-and-sent,
    or a payload we refused. Never an error the caller has to handle.
    """
    if phase not in _BUILDERS:
        raise ValueError(f"{phase!r} is not a record phase; use record_attachment")

    if not is_syncable(attempt):
        return None
    attempt = concrete(session, attempt)

    try:
        values = envelope_values(attempt, strict=True)
    except EnvelopeError as exc:
        # Distinguish "no Airtable origin" from "linked but broken". The first
        # is a normal local job; the second is our bug and must stay visible.
        if _unlinked(attempt):
            attempt.airtable_sync_state = EXCLUDED
            return None
        # A linked job with an incomplete binding: nobody has finished attaching
        # the protocol or section. A retry cannot fix that on its own, so it is
        # recorded as **not** recoverable — someone must bind it first.
        return _refuse(session, attempt, phase, exc, recoverable=False)

    kwargs = {}
    if phase in (TERMINAL, VERDICT):
        # The terminal and verdict builders assert the status is terminal, so
        # pass what the attempt actually reached rather than the default.
        kwargs["status"] = attempt.status
    try:
        payload = _BUILDERS[phase](values, **kwargs)
    except EnvelopeError as exc:
        # A payload our own envelope rejected: a defect on our side, and
        # recoverable once the defect is fixed and the phase re-queued.
        return _refuse(session, attempt, phase, exc, values=values)

    entry = outbox.enqueue(session, attempt.labos_attempt_id, phase, payload,
                           payload_updated_at=attempt.labos_updated_at)
    # Queued successfully, so any recorded failure for this phase is repaired.
    # Resolved here rather than by the repair route, so a phase that starts
    # working on its own does not leave a stale open failure in the status.
    for row in outbox.open_publication_failures(session):
        if row.attempt_id == attempt.labos_attempt_id and row.phase == phase:
            outbox.resolve_publication_failure(session, row)
    return entry


def record_attachment(session, attempt, photo):
    """Queue one photograph for delivery. Returns the entry or None.

    **One entry per photograph, not one per attempt.** Contract §6 makes a
    single sender own one artifact at a time and requires the returned
    attachment ids to be recorded, both of which are per-file facts. It also
    removes two problems the per-attempt snapshot had: an attempt with no
    photographs queued an entry carrying nothing, which showed up as permanent
    attachment backlog; and a photograph legally added between termination and
    review — which `_save_photo` allows, and contract §6 permits until the
    verdict — was missed by a set snapshotted at termination.

    The freeze point is therefore stated once, here: **evidence may be added
    until the verdict, and each addition queues its own delivery.** After the
    verdict `_save_photo` returns 409 and nothing more is ever queued.
    """
    if not is_syncable(attempt):
        return None
    attempt = concrete(session, attempt)
    if _unlinked(attempt):
        attempt.airtable_sync_state = EXCLUDED
        return None

    payload = {
        "LabOS Attempt ID": attempt.labos_attempt_id,
        "photo": {
            "id": photo.id,
            "filename": photo.filename,
            "path": photo.path,
            "note": photo.note,
            # Which impact this evidences, or null for attempt-level evidence.
            "shot_id": photo.shot_id,
        },
    }
    return outbox.enqueue(session, attempt.labos_attempt_id, ATTACHMENT, payload,
                          payload_updated_at=attempt.labos_updated_at)


def _unlinked(attempt):
    """True when this attempt has no Airtable origin at all.

    Checked on the *project*, because that is where a locally-created job is
    distinguishable: a job imported from Airtable always has a project id, and
    one an operator typed never does. A missing protocol or section id on an
    otherwise-linked job is a different thing — a binding someone has not
    finished — and must not be silently excluded.
    """
    from ..airtable.mapping import owning_test
    test = owning_test(attempt)
    project = getattr(test, "project", None) if test is not None else None
    return project is None or not project.airtable_project_id


def _refuse(session, attempt, phase, exc, values=None, recoverable=True):
    """Record a payload we would not send, durably. Never raises.

    Three places now, and each is load-bearing:

    * on the **attempt**, so a screen showing one result shows its sync state;
    * in **`sync_publication_failure`**, so it survives, appears in
      `GET /sync/status`, and has somewhere to be repaired from. Until
      2026-09-08 only the attempt column was written and nothing read it — the
      headline said `Synced` for an attempt that had never been published,
      because status computes from the queue and there was no queue entry;
    * in the **log**, for whoever is watching the container.

    `values` is the envelope input that was refused, snapshotted. Repair must not
    re-derive it: by then the attempt may have been reviewed, and rebuilding
    would repair a different phase from the one that failed.
    """
    attempt.airtable_sync_state = "Sync Failed"
    attempt.airtable_sync_error = f"{phase}: {exc}"
    outbox.record_publication_failure(
        session, attempt.labos_attempt_id, phase, exc,
        payload_snapshot=_snapshot(values),
        payload_updated_at=attempt.labos_updated_at,
        recoverable=recoverable)
    log.error("refused to queue %s for attempt %s: %s",
              phase, attempt.labos_attempt_id, exc)
    return None


def _snapshot(values):
    """The refused values, JSON-safe. Datetimes become ISO strings."""
    if not values:
        return None
    out = {}
    for k, v in values.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, (str, int, float, bool, type(None), list, dict)):
            out[k] = v
        else:
            out[k] = repr(v)
    return out
