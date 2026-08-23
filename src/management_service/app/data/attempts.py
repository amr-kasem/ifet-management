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

# Marks attempts that predate the integration. Set by the P1 migration on the
# 623 rows that existed then, and never set on a new attempt.
EXCLUDED = "Excluded"
PENDING = "Pending"

IN_PROGRESS = "In Progress"
COMPLETED = "Completed"
ABORTED = "Aborted"
TERMINAL_STATUSES = (COMPLETED, ABORTED)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def test_id_for(existing_trials):
    """The `labos_test_id` a new attempt at this test must carry.

    Reuses the id already on a sibling attempt; mints one only for the first
    attempt at a test. Historical attempts backfilled by the migration count as
    siblings, so a re-run of an old test correctly joins its existing group
    rather than starting a new one.
    """
    for trial in existing_trials or ():
        existing = getattr(trial, "labos_test_id", None)
        if existing:
            return existing
    return str(uuid.uuid4())


def begin(existing_trials, *, test_type, test_name=None, operator_name=None,
          test_rig=None, schema_version=None, now=None):
    """Identity + lifecycle kwargs for a newly created attempt.

    Returned as a dict rather than applied to an object, so the caller's model
    constructor stays the single place an attempt is built.
    """
    now = now or _now()
    return {
        "labos_attempt_id": str(uuid.uuid4()),
        "labos_test_id": test_id_for(existing_trials),
        "schema_version": schema_version,
        "status": IN_PROGRESS,
        "test_type": test_type,
        "test_name": test_name,
        "operator_name": operator_name,
        "test_rig": test_rig,
        "retest_required": False,
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
