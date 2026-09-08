"""Attempt identity and lifecycle (P1 / Ref 46, write contract v0.3 §3).

Every attempt needs two identifiers and they mean different things:

* `labos_attempt_id` — unique to THIS attempt. Airtable upserts on it, so it is
  the merge key and must never be reused.
* `labos_test_id` — shared by every attempt at the SAME test. This is what makes
  "attempt 2 of this test" expressible; without it, attempts are unrelated rows.

Kept out of `main.py` so the endpoints stay readable and so the rule about
sharing a test id lives in exactly one place — the failure mode of getting it
wrong (every attempt minting a fresh test id) produces data that looks perfectly
valid and quietly makes retest counting impossible.
"""

import datetime as dt
import uuid

from sqlalchemy.exc import IntegrityError

# Marks attempts that predate the integration. Set by the P1 migration on the
# 623 rows that existed then, and never set on a new attempt.
EXCLUDED = "Excluded"
PENDING = "Pending"

IN_PROGRESS = "In Progress"
COMPLETED = "Completed"
ABORTED = "Aborted"
TERMINAL_STATUSES = (COMPLETED, ABORTED)


# The namespace the P1 migration used for its backfill. **It must stay
# byte-identical to `b7c2e9a41d38`'s `_NS`**, because that migration and this
# module must derive the same id for the same test — otherwise a historical
# test and a new attempt at it end up in different groups, which is the one
# thing `labos_test_id` exists to prevent.
_NS = uuid.UUID("5f2b1c94-3a7e-4d18-9c60-1e8a7d2f4b03")

# The `kind` token per parent table. Static and cyclic match the strings the P1
# backfill derived from its child table names, so their ids are reproduced
# exactly rather than re-minted.
STATIC, CYCLIC, MANUAL, IMPACT = "static", "cyclic", "manual", "impact"


def _now():
    return dt.datetime.now(dt.timezone.utc)


def test_id_for_test(kind, parent_id):
    """The `labos_test_id` for a test, derived rather than minted.

    **One allocator for all five test types, from 2026-09-08.** There were
    three: this deterministic `uuid5` in the P1 backfill, a random `uuid4` in
    `test_id_for()` below, and a readable slug (`impact-7`) in `main.py` for the
    three manual types. Three formats in one Airtable column, in the field the
    Airtable team asked to use as their retest grouping key.

    Deterministic rather than random, for three reasons that all point the same
    way: it reproduces the backfill's value for a historical test, so old and
    new attempts group together without a sibling lookup; re-running any
    backfill is idempotent; and it needs no extra column and survives a restart.

    Opaque on purpose — a UUID, not a slug. The slug leaked a database primary
    key into a customer's system and invited parsing. The change document tells
    them to group on equality and never parse; this is us keeping that true from
    our side rather than asking them to.
    """
    return str(uuid.uuid5(_NS, f"{kind}:{parent_id}"))


def test_id_for(existing_trials):
    """The `labos_test_id` a new attempt at this test must carry — legacy path.

    Reuses the id already on a sibling attempt, which is what keeps a re-run of
    a backfilled test in its existing group. Retained as a **fallback** for
    callers that cannot name their parent: it mints a random `uuid4`, which is
    valid but not reproducible, so `test_id_for_test()` is preferred everywhere
    the parent is known — which, since 2026-09-08, is everywhere.
    """
    for trial in existing_trials or ():
        existing = getattr(trial, "labos_test_id", None)
        if existing:
            return existing
    return str(uuid.uuid4())


def begin(existing_trials, *, test_type, test_name=None, operator_name=None,
          test_rig=None, schema_version=None, now=None, kind=None, parent_id=None):
    """Identity + lifecycle kwargs for a newly created attempt.

    Returned as a dict rather than applied to an object, so the caller's model
    constructor stays the single place an attempt is built.
    """
    now = now or _now()
    return {
        "labos_attempt_id": str(uuid.uuid4()),
        # Prefer the derived id; fall back to the sibling lookup only when the
        # caller could not name its parent.
        "labos_test_id": (test_id_for_test(kind, parent_id)
                          if kind and parent_id is not None
                          else test_id_for(existing_trials)),
        "schema_version": schema_version,
        "status": IN_PROGRESS,
        "test_type": test_type,
        "test_name": test_name,
        "operator_name": operator_name,
        "test_rig": test_rig,
        # **Not `False`.** §6: "Retest Required is meaningful only once review
        # exists, never inferred false from an unreviewed checkbox." Stamping
        # False at creation is exactly that, and it made the two initialisation
        # paths behave differently: `_start_attempt` (manual and impact) left it
        # NULL, so their terminal payload omitted the key; this path set it, so
        # the envelope's phase guard refused every rig terminal write —
        # `'Retest Required' belongs to the first-review phase`. The rig types
        # therefore never queued a terminal at all.
        #
        # Left out entirely, so the column keeps its NULL default and both paths
        # agree. `d1a6b93f2e57` widened it to nullable for this reason; this line
        # predated that and was not revisited.
        "testing_start_date": now,
        "labos_created_at": now,
        "labos_updated_at": now,
        # New attempts are eligible for sync; only pre-integration rows are not.
        "airtable_sync_state": PENDING,
    }


def mark_terminal(attempt, status, *, test_result=None, abort_reason=None,
                  testing_continued="Stopped", now=None):
    """Move an attempt to its final state.

    Refuses to move an already-terminal attempt. Contract §3 makes the terminal
    state final: a corrected result is a NEW attempt that names the one it
    supersedes, never an edit of the original. Allowing a second transition here
    would destroy the evidence a certification report was issued against.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"{status!r} is not a terminal status {TERMINAL_STATUSES}")
    if attempt.terminal_at is not None:
        raise ValueError(
            f"attempt {attempt.labos_attempt_id!r} is already terminal "
            f"({attempt.status!r} at {attempt.terminal_at.isoformat()}). Contract §3: "
            "record a correction as a NEW attempt with corrects_attempt_id set, "
            "rather than editing this one."
        )

    now = now or _now()
    attempt.status = status
    attempt.test_result = test_result
    attempt.abort_reason = abort_reason
    attempt.testing_continued = testing_continued
    attempt.testing_end_date = now
    attempt.terminal_at = now
    attempt.labos_updated_at = now
    return attempt


def as_correction(attempt, supersedes, reason):
    """Mark a new attempt as superseding an earlier one (contract §3.1).

    `supersedes` is the earlier attempt's `labos_attempt_id`, not its row id —
    the reference has to survive into Airtable, where the integer primary key
    means nothing.
    """
    if not reason or not str(reason).strip():
        raise ValueError("a correction must state why (contract §4.1)")
    if supersedes == attempt.labos_attempt_id:
        raise ValueError("an attempt cannot supersede itself")
    attempt.corrects_attempt_id = supersedes
    attempt.correction_reason = reason
    return attempt


# How many times attempt-number allocation retries a collision before giving up.
# Each retry is one lost race against another start of the same test; more than
# a couple means something other than concurrency is wrong.
ALLOCATE_MAX_RETRIES = 5


def insert_attempt(session, build, *, max_retries=ALLOCATE_MAX_RETRIES):
    """Insert an attempt, re-allocating its number if another start took it.

    `build()` must return a new, unadded attempt object, numbering itself with
    `next_attempt_number()`. It is called once per try, because the object of a
    failed insert cannot be reused and the number must be re-read.

    **Why this exists.** `trial_number` was allocated `count()+1`, which is
    correct in sequence and unprotected against two starts at once — an operator
    double-pressing Start, or a rig callback arriving while the UI posts. Both
    attempts then carried the same Attempt Number under different
    `LabOS Attempt ID`s, and Airtable would show two records that read as one
    duplicated. `UniqueConstraint(labos_test_id, trial_number)` now makes that
    impossible; this function is what turns the constraint from a 500 into a
    correctly-numbered second attempt.

    Same shape and the same caveat as `sync.outbox.enqueue`: the savepoint is a
    Postgres mechanism. On SQLite `begin_nested()` + `flush()` commits outright,
    so the retry is skipped there — SQLite has a single writer and no race to
    protect against.
    """
    postgres = session.bind is not None and session.bind.dialect.name == "postgresql"

    if not postgres:
        obj = build()
        session.add(obj)
        return obj

    last_error = None
    for _ in range(max_retries):
        obj = build()
        try:
            with session.begin_nested():
                session.add(obj)
                session.flush()
            return obj
        except IntegrityError as exc:
            last_error = exc
            # **Do not expunge unconditionally.** Exiting `begin_nested()` on an
            # exception rolls the savepoint back, and that rollback already
            # discards the pending insert — so `expunge` raised
            # `InvalidRequestError: Instance ... is not present in this Session`
            # *instead of* retrying. The constraint still prevented corruption,
            # but a genuine collision became a 500 rather than attempt N+1,
            # which is the whole point of this loop. Verified 2026-09-08: the
            # builder ran once, not five times.
            if obj in session:
                session.expunge(obj)
    raise RuntimeError(
        "could not allocate an attempt number after "
        f"{max_retries} attempts"
    ) from last_error


def next_attempt_number(session, labos_test_id):
    """One past the highest Attempt Number already used for this test.

    Keyed on `labos_test_id` rather than the per-subclass foreign key, so one
    query serves all five test types — and it is the same column the uniqueness
    constraint uses, so what is read is exactly what is enforced.
    """
    from .models import TestResult
    from sqlalchemy import func
    return (session.query(func.coalesce(func.max(TestResult.trial_number), 0))
            .filter(TestResult.labos_test_id == labos_test_id)
            .scalar()) + 1


def complete_rig_trial(attempt, body, now=None, test_operator=None):
    """Terminate a rig-posted stage, if the post carried enough to terminate.

    **Why this exists.** Static and cyclic post a *finished* stage in one call,
    but the route recorded it as `In Progress` and never terminated it — so the
    terminal phase could not be built and only `create` was ever queued. The
    three manual types go through `PUT /test-results/{id}/finish`; the rig types
    had no equivalent, which is how "all five types" was true of identity and
    false of the lifecycle.

    Returns True when the attempt was terminated. Returns False, leaving it
    `In Progress`, only when **no operator is known from either source** — the
    callback or the run start. Contract §4.5 requires an operator on a terminal
    write and LabOS does not invent one; that refusal surfaces in
    `GET /sync/failures` rather than as silence.
    """
    # The callback's operator if it sent one, otherwise the one declared at run
    # start. The second is the normal case for a real rig: firmware sends
    # `deflections` alone and does not need to change, because the operator is
    # not a fact the rig has — see `a3d8e5c71f04`.
    operator = getattr(body, "operator_name", None) or test_operator
    if not operator:
        return False
    now = now or _now()
    attempt.operator_name = operator
    attempt.status = COMPLETED
    attempt.result = body.result if body.result is not None else attempt.result
    attempt.testing_continued = (body.testing_continued
                                 or attempt.testing_continued or "Stopped")
    attempt.note = body.note or attempt.note
    attempt.testing_end_date = now
    attempt.terminal_at = now
    attempt.labos_updated_at = now
    return True


def open_attempt_for(session, cls, fk_name, test_id):
    """The In Progress attempt at this test, or None. At most one can exist.

    An attempt is open until `terminal_at` is stamped. Keyed on the parent test,
    because "is this test already running" is the question a duplicate Start
    asks.
    """
    from .models import TestResult
    return (session.query(cls)
            .filter(getattr(cls, fk_name) == test_id,
                    TestResult.terminal_at.is_(None))
            .order_by(cls.id.desc())
            .first())


def rig_is_busy(session, device_id, exclude_test=None):
    """Another test is mid-run on this rig. Returns the offending attempt or None.

    **One active physical run per rig is a hardware fact, not a policy.** One rig
    holds one specimen and runs one test at a time (plan §4.3). So an attempt
    starting on a rig that already has an open attempt at a *different* test is
    not a concurrency case to be serialised — it is a request that cannot
    correspond to anything physical, and refusing it is the only honest answer.
    """
    from .models import (CyclicTestResult, ImpactTestResult, ManualTestResult,
                         Project, StaticTestResult, TestResult)
    from .models import CyclicTest, ManualTest, MissileImpactTest, StaticTest

    pairs = ((StaticTestResult, "static_test_id", StaticTest),
             (CyclicTestResult, "cyclic_test_id", CyclicTest),
             (ManualTestResult, "manual_test_id", ManualTest),
             (ImpactTestResult, "missile_impact_test_id", MissileImpactTest))
    for cls, fk, parent in pairs:
        q = (session.query(cls)
             .join(parent, getattr(cls, fk) == parent.id)
             .join(Project, parent.project_id == Project.id)
             .filter(Project.device_id == device_id,
                     TestResult.terminal_at.is_(None)))
        if exclude_test is not None:
            same_table = parent.__tablename__ == exclude_test[0]
            if same_table:
                q = q.filter(getattr(cls, fk) != exclude_test[1])
        found = q.first()
        if found is not None:
            return found
    return None


def capture_cycles_completed(attempt, test, now=None):
    """Snapshot the rig-reported cycle count onto the attempt, at termination.

    **Where the number comes from, exactly.** `cyclic_tests.cycles` is the
    configured *target* and is never published as an achievement.
    `cyclic_tests.current_cycle` is progress the **rig reports during the run**,
    through `PUT .../update_status` (`main.py`: `cyclic_test.current_cycle =
    data.current_cycle`). That is confirmed execution evidence, and it is the
    source.

    **Why it is snapshotted and not read at publish time.** `current_cycle` lives
    on the *test*, and two routes zero it — `reset`, and `finish` itself. So by
    the time a payload is built the evidence may already be gone, and a second
    run at the same test would overwrite the first attempt's count with its own.
    Reading it live was the first fix and it was wrong for both reasons. Captured
    here, at the moment of termination, onto the attempt's own column.

    Left as None when the rig reported nothing: §6 forbids substituting zero for
    unknown, and a run that completed no cycles is a different claim from one we
    cannot account for.
    """
    done = getattr(test, "current_cycle", None)
    if done:
        attempt.cycles_completed = done
    return attempt.cycles_completed
