#!/bin/bash
#
# Container entrypoint: wait for Postgres, apply migrations, serve.
#
# ---------------------------------------------------------------------------
# What changed on 2026-08-23, and why (LabOS<->Airtable P1, Epic IFET-32)
#
# This script used to call `command.revision(autogenerate=True)` before every
# upgrade, so each container start diffed models.py against the live database
# and wrote a migration for whatever it found. Because `alembic/versions` is
# bind-mounted from the node and gitignored, those files accumulated on the node
# and were invisible to git: 29 revisions in production, 28 of them empty
# no-ops, one per restart since January 2026.
#
# The real problem was not the clutter. It was that a models.py change reaching
# the node would ALTER THE PRODUCTION SCHEMA at the next restart, with no
# migration review and nobody in the loop -- on a database whose contents are
# certification evidence.
#
# So: migrations are now applied, never invented. A schema change must arrive as
# a reviewed migration file in alembic/versions. If models.py and the database
# disagree and no migration says how to reconcile them, that is a bug to be
# seen, not something for a container to guess at during boot.
#
# Failure is also fatal now. Previously both alembic calls were wrapped in
# try/except and the script continued regardless -- and the `if [ $? -eq 0 ]`
# check below it could never fire, because the caught exceptions meant python
# always exited 0. The API would then serve against a half-migrated schema and
# write data into it. Exiting non-zero instead means `restart: always` retries,
# and a genuinely broken migration is loud rather than silent.
# ---------------------------------------------------------------------------

set -euo pipefail

DATABASE_URL="${DATABASE_URL:-postgresql://user:password@localhost/report_db}"

echo "Waiting for the database to accept connections..."
DATABASE_URL="$DATABASE_URL" python - <<'PY'
import os
import sys
import time

from sqlalchemy import create_engine, text

url = os.environ["DATABASE_URL"]
deadline = time.monotonic() + 120
attempt = 0

# A real readiness check rather than `sleep 10`. The fixed sleep was a guess
# that happened to be long enough; with migration failure now fatal, a slow
# database must not be indistinguishable from a broken migration.
while True:
    attempt += 1
    try:
        create_engine(url).connect().execute(text("SELECT 1"))
        print(f"Database ready after {attempt} attempt(s).")
        break
    except Exception as exc:
        if time.monotonic() >= deadline:
            print(f"Database not reachable after 120s: {exc}", file=sys.stderr)
            sys.exit(1)
        time.sleep(2)
PY

echo "Applying database migrations..."
DATABASE_URL="$DATABASE_URL" python - <<'PY'
import os

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

url = os.environ["DATABASE_URL"]
config = Config("alembic.ini")
config.set_main_option("sqlalchemy.url", url)

# Say what is about to happen before doing it, so the container log is a usable
# record of when the schema changed and to what.
with create_engine(url).connect() as conn:
    current = MigrationContext.configure(conn).get_current_revision()
head = ScriptDirectory.from_config(config).get_current_head()

print(f"  database at: {current}")
print(f"  head is:     {head}")

if current == head:
    print("  nothing to apply.")
else:
    print("  applying...")

# No try/except: a failure here must stop the container rather than let the API
# serve against a schema nobody has verified.
command.upgrade(config, "head")
print("Migrations applied successfully.")
PY

echo "Starting FastAPI application..."
exec /start.sh
