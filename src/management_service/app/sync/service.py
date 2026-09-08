"""The sync worker as a runnable process — delivery plan DG6, deployment half.

Contract §7 said "one container, no public port" and stopped there: no compose
service, no environment variables, no health policy, in a design that specifies
fencing semantics to the sentence. This module is that missing half.

What it is, precisely:

*   **One process, one slot.** It takes the advisory lock in `singleton.py`
    before it does anything else, with `required=True`. A second instance exits
    non-zero with a clear message instead of racing the first.
*   **No public port.** It serves nothing. Liveness is the `worker_heartbeat_at`
    row that `GET /sync/status` already reads, which is deliberate: a worker
    that answered its own health check would report healthy from inside a
    process whose database connection had gone.
*   **It reads its settings through `app.config`**, never `os.getenv`, so the
    Airtable token stays out of logs and the one-table write allowlist applies.
*   **It stops cleanly on SIGTERM**, finishing the cycle in flight rather than
    abandoning a leased entry. A `docker stop` therefore costs at most one
    cycle, not a lease timeout.

Run it with `python -m app.sync.service`.
"""

import logging
import os
import signal
import sys
import time

from ..airtable.errors import AirtableValidationError
from . import outbox
from . import singleton
from . import worker as sync_worker

log = logging.getLogger("app.sync.service")

# Seconds between cycles when the queue was empty. Short enough that a result
# reaches Airtable promptly, long enough not to hammer an idle database.
DEFAULT_IDLE_INTERVAL = 5.0
# When a cycle delivered something there is probably more, so go straight round.
DEFAULT_BUSY_INTERVAL = 0.0


class Stopping:
    """SIGTERM/SIGINT flag. Set once, read between cycles."""

    def __init__(self):
        self.requested = False

    def install(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except ValueError:                              # pragma: no cover
                pass                # not on the main thread (tests)
        return self

    def _handle(self, signum, _frame):
        log.info("signal %s received; finishing the current cycle", signum)
        self.requested = True


def run(session_factory, send, *, slot_engine, idle_interval=DEFAULT_IDLE_INTERVAL,
        busy_interval=DEFAULT_BUSY_INTERVAL, stopping=None, max_cycles=None,
        sleep=time.sleep):
    """The loop. Injected everywhere, so the tests drive it without a network.

    Returns the number of cycles run. Holds the worker slot for the whole loop
    and releases it on the way out - including on an exception, so a crash frees
    the slot for the replacement container immediately.
    """
    stopping = stopping or Stopping()
    cycles = 0
    with singleton.acquire(slot_engine, required=True) as slot:
        log.info("sync worker started (slot enforced=%s)", slot.enforced)
        while not stopping.requested:
            if max_cycles is not None and cycles >= max_cycles:
                break
            session = session_factory()
            try:
                result = sync_worker.run_cycle(session, send)
            except Exception:
                # Never let one bad cycle kill the worker: the queue is durable,
                # and a crash-loop would look like an outage while the fix is a
                # retry. The error is recorded in sync_state by run_cycle where
                # it can be, and logged here where it cannot.
                log.exception("sync cycle failed")
                session.rollback()
                result = None
            finally:
                session.close()
            cycles += 1

            if result is not None and result.discarded:
                # Only reachable if the single-worker guarantee was bypassed.
                log.warning("%s outcome(s) discarded: the entry was re-claimed "
                            "mid-send, so this process is not the only worker",
                            result.discarded)

            busy = result is not None and (result.delivered or result.claimed)
            sleep(busy_interval if busy else idle_interval)
    log.info("sync worker stopped after %s cycle(s)", cycles)
    return cycles


def main(argv=None):                                        # pragma: no cover
    """Entry point. Wires the real database and the real Airtable client."""
    logging.basicConfig(
        level=os.environ.get("SYNC_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Imported here, not at module scope, so importing this module stays
    # side-effect-free - `app/` is bind-mounted into report-api and gets
    # imported there too.
    #
    # And built here rather than imported from `app.main`: that module *is* the
    # FastAPI application, and importing it to borrow its engine would start 25
    # routes and create directories inside the worker. `app/utils/*.py` already
    # build their own engine the same way.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from ..airtable.client import AirtableClient
    from ..config import airtable_settings

    # A misconfiguration, unlike the two below: something intended to run cannot.
    # Non-zero, so the restart policy retries it - a compose ordering problem or
    # a missing .env entry is worth retrying a few times.
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL is not set; refusing to guess a database")
        return 2
    # The two "deliberately off" states exit **0**, and that is the whole point.
    #
    # Both are valid: the stack must start before the Airtable team issues a
    # token, and sync stays dark until the round-trip is approved. Exiting
    # non-zero would make `restart: on-failure` restart the container five times
    # over, burying the single log line that explains why it is not running -
    # the exact failure mode the restart policy exists to avoid. A container that
    # has correctly decided it has nothing to do should stop, once, quietly.
    if not airtable_settings.is_configured:
        log.info("Airtable is not configured (token/base/table); nothing to do")
        return 0
    if not airtable_settings.sync_enabled:
        log.info("AIRTABLE_SYNC_ENABLED is not true; the worker stays off")
        return 0

    engine = create_engine(database_url, pool_pre_ping=True)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    client = AirtableClient(settings=airtable_settings)

    def send(entry):
        """One outbox entry -> one upsert on the single writable table.

        The payload is already the envelope: `enqueue` stored what the mapping
        produced, so the worker never re-derives it. Re-deriving at send time
        would mean the row that goes out is whatever the database says *now*,
        not what was agreed when the phase was recorded.

        **Attachment entries are refused here, deliberately.** This function
        used to send every phase through `upsert_records` without looking at
        `entry.phase`, and an attachment payload is not a record payload — it
        carries a `photo` object, not Airtable fields. Live, Airtable would have
        rejected every photograph as an unknown field: a 422, which is terminal,
        so **every attachment would have parked on first contact** and
        `/sync/status` would have sat at Retry Required forever. No test could
        see it, because the suites inject a sender that accepts any payload.
        The upload path (preview generation, contract §6's direct upload,
        recording the returned attachment ids) is not built yet.
        """
        if entry.phase == outbox.ATTACHMENT:
            raise AirtableValidationError(
                "attachment delivery is not implemented yet: LabOS has no "
                "preview-generation or upload path, so this photograph cannot "
                "be sent. Parked deliberately rather than PATCHed as record "
                "fields, which Airtable would reject as an unknown field. The "
                "evidence is safe in LabOS and this entry carries what to send "
                "once the uploader exists."
            )
        response = client.upsert_records(
            airtable_settings.results_table, [entry.payload]
        )
        records = response.get("records") or []
        return records[0].get("id") if records else None

    stopping = Stopping().install()
    try:
        run(session_factory, send, slot_engine=engine, stopping=stopping)
    except singleton.WorkerAlreadyRunning as exc:
        log.error("%s", exc)
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    sys.exit(main())
