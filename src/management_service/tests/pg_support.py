"""Test plumbing shared by the outbox suites.

One decision lives here: **where the outbox tests get their database.** Set
`M2_DATABASE_URL` and they run against real Postgres; leave it unset and they
run against SQLite in memory as before.

That switch exists because the §7.1 guarantees are *invisible* in SQLite:
`FOR UPDATE SKIP LOCKED` is silently ignored, two connections cannot contend for
a row, and a savepoint retry has nothing to race against. A suite that passes
only on SQLite has not tested exclusive claim, fencing or sequence allocation -
it has tested that the code runs. So the delivery plan does not accept
SQLite-only results as proof of any of them.

Isolation differs by backend, deliberately. SQLite in memory is a fresh database
per connection, so nothing needs cleaning. Postgres is one durable database for
the whole run, so the sync tables are created once and truncated between tests -
which is also faster than recreating the legacy schema 23 times.
"""

import os

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.data.models import Base
from app.sync.outbox import SyncAttemptState, SyncOutbox
from app.sync.state import SyncState

SQLITE_URL = "sqlite://"

# Only these three. Creating the whole legacy schema would make every setUp pay
# for tables no outbox test touches.
SYNC_TABLES = [SyncOutbox.__table__, SyncAttemptState.__table__, SyncState.__table__]


def database_url():
    return os.environ.get("M2_DATABASE_URL") or SQLITE_URL


def is_postgres():
    return database_url().startswith("postgresql")


def make_engine(**kwargs):
    """An engine for the configured backend.

    On Postgres, `pool_pre_ping` because the harness container may outlive a
    database restart, and a stale pooled connection would fail the next test
    rather than the thing under test.
    """
    url = database_url()
    if url.startswith("postgresql"):
        kwargs.setdefault("pool_pre_ping", True)
    return create_engine(url, **kwargs)


def prepare(engine):
    """Create the sync tables if needed, and leave them empty."""
    Base.metadata.create_all(engine, tables=SYNC_TABLES)
    truncate(engine)


# A TRUNCATE needs ACCESS EXCLUSIVE, so it queues behind any open transaction
# holding a row in these tables. Left unbounded that turns one leaked session
# into a suite that hangs instead of failing - which is exactly what happened on
# 2026-09-06, when a killed `docker compose run` left its container alive with a
# transaction open and every later run blocked on setUp.
TRUNCATE_LOCK_TIMEOUT_MS = 5000


def truncate(engine):
    if not is_postgres():
        return
    names = ", ".join(t.name for t in SYNC_TABLES)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"SET LOCAL lock_timeout = '{TRUNCATE_LOCK_TIMEOUT_MS}ms'")
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY"))
    except OperationalError as exc:
        raise AssertionError(
            "could not clear the sync tables: something is holding a lock on "
            "them. Almost always a leaked test container or session - check\n"
            "  docker compose -f src/management_service/tests/postgres_harness/"
            "docker-compose.yaml ps -a\n"
            "and\n"
            "  SELECT pid, state, wait_event_type, query FROM pg_stat_activity;\n"
            f"underlying error: {exc}"
        ) from exc


def session_factory(engine):
    return sessionmaker(bind=engine)


def skip_unless_postgres(test):
    """Decorator for the guarantees that only exist on Postgres."""
    import unittest
    return unittest.skipUnless(
        is_postgres(),
        "needs Postgres - set M2_DATABASE_URL (see tests/postgres_harness/)",
    )(test)
