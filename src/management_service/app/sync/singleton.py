"""Exactly one sync worker, enforced by the database.

The decision (2026-09-06): **one worker, and enforced rather than conventional.**

"We only deploy one" is not a guarantee, it is a habit, and habits fail in the
ways that are hardest to see: a rolling restart where the old container drains
while the new one starts, a stray `docker compose up -d`, an engineer running the
worker by hand to debug a stuck queue. This fleet already shows what that leaves
behind - system-2 carries four disabled legacy systemd units.

So a second worker does not race the first, and does not quietly double-send. It
**refuses to start.**

The mechanism is a Postgres session-level advisory lock, chosen because of how it
is released: it lives on one connection and disappears when that connection
does. Kill the worker with SIGKILL, lose its network, `docker rm -f` it - the
lock is gone the moment the socket closes. There is no stale lock file, no TTL to
tune, and no expiry to get wrong. That is the opposite of a lease, and the reason
this is a better fit than a row in a table.

It does not replace `owner_epoch` or `SELECT ... FOR UPDATE SKIP LOCKED` in
`outbox.py`. Those cost nothing at one worker and they are what make the topology
question *stop mattering* - see delivery plan DG6. This makes the intended
topology true; those make a violation of it harmless.
"""

import logging
import zlib

from sqlalchemy import text

log = logging.getLogger(__name__)

# A stable 32-bit key derived from a name, so it is self-documenting and cannot
# collide by accident with another advisory lock someone adds later.
LOCK_NAME = "ifet.sync.worker"
LOCK_KEY = zlib.crc32(LOCK_NAME.encode("ascii"))


class WorkerAlreadyRunning(RuntimeError):
    """Another process already holds the worker slot."""


class WorkerSlot:
    """The held slot. Keeps its own connection open for exactly as long as it.

    Use as a context manager. Releasing closes the connection, which is what
    releases the lock - so there is no path where the lock outlives the holder.
    """

    def __init__(self, connection, enforced=True):
        self._connection = connection
        # False on a backend with no advisory locks (SQLite in the unit tests).
        # Recorded rather than hidden, so a caller can log that the guarantee is
        # not in force instead of assuming it is.
        self.enforced = enforced

    def release(self):
        if self._connection is not None:
            try:
                if self.enforced:
                    self._connection.exec_driver_sql(
                        "SELECT pg_advisory_unlock(%s)", (LOCK_KEY,)
                    )
            except Exception:                                # pragma: no cover
                # The connection is about to close, which releases the lock
                # anyway. Never let cleanup mask the original error.
                log.debug("advisory unlock failed; closing anyway", exc_info=True)
            finally:
                self._connection.close()
                self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def acquire(engine, required=None):
    """Take the worker slot, or raise `WorkerAlreadyRunning`.

    `required` forces the outcome when the backend cannot enforce it: leave it
    None to allow an unenforced slot on SQLite (so unit tests can run the loop),
    pass True to insist on real enforcement and fail loudly otherwise. The
    service passes True, because a production worker that silently ran without
    the guarantee would be worse than one that would not start.
    """
    dialect = engine.dialect.name
    if dialect != "postgresql":
        if required:
            raise WorkerAlreadyRunning(
                f"the worker slot cannot be enforced on {dialect!r}; "
                "Postgres advisory locks are the mechanism"
            )
        log.warning("worker slot NOT enforced on %s - no advisory locks", dialect)
        return WorkerSlot(engine.connect(), enforced=False)

    connection = engine.connect()
    try:
        got = connection.exec_driver_sql(
            "SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)
        ).scalar()
    except Exception:
        connection.close()
        raise
    if not got:
        connection.close()
        raise WorkerAlreadyRunning(
            "another sync worker holds the slot; refusing to start a second one. "
            f"(advisory lock {LOCK_NAME}/{LOCK_KEY})"
        )
    log.info("sync worker slot acquired (%s/%s)", LOCK_NAME, LOCK_KEY)
    return WorkerSlot(connection, enforced=True)


def holder_count(engine):
    """How many connections hold the slot. 0 or 1; anything else is a bug.

    Read from `pg_locks` rather than tracked in Python, so it reports what is
    actually true of the database and not what this process believes.
    """
    if engine.dialect.name != "postgresql":
        return None
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM pg_locks "
                 "WHERE locktype = 'advisory' AND objid = :key AND granted"),
            {"key": LOCK_KEY},
        ).scalar()
