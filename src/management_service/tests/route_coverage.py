"""Which of the manual-test routes does the suite actually exercise?

    docker compose -f tests/postgres_harness/docker-compose.yaml \\
        run --rm -e PYTHONPATH=/app tests python tests/route_coverage.py

A passing suite says the code it calls works. It says nothing about the code it
never calls, and "are all the endpoints tested?" is not answerable by reading
the test names — several routes here differ only in their prefix.

Run on 2026-09-08 it found 13 of 18 exercised: both list routes, the impact
test-level finish, and both pre-existing `/test-results/{id}` routes had no
test at all. Writing one for `PUT /test-results/{id}` then found a second
hard-coded `Path("uploads")` inside it, which would have diverged from
LABOS_UPLOADS_DIR and written evidence somewhere the static mount does not
serve.

Exits non-zero if any route is unexercised, so it can gate a change that adds
one.
"""
import sys, unittest

HIT = set()

from app import main  # noqa: E402

@main.app.middleware("http")
async def _record(request, call_next):
    resp = await call_next(request)
    r = request.scope.get("route")
    if r is not None:
        HIT.add((request.method, r.path))
    return resp

loader = unittest.TestLoader()
suite = loader.discover("tests", pattern="test_manual_tests.py")
res = unittest.TextTestRunner(verbosity=0).run(suite)

NEW_PREFIXES = ("manual-test", "impact-test", "/shots", "test-results")
new = set()
for route in main.app.routes:
    path = getattr(route, "path", "")
    if any(k in path for k in NEW_PREFIXES):
        for m in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
            new.add((m, path))

print(f"\ntests: {res.testsRun} run, {len(res.failures)} failed, {len(res.errors)} errors")
print(f"new-surface routes: {len(new)}\n")
covered = sorted(new & HIT)
missed  = sorted(new - HIT)
for m, p in covered:
    print(f"  HIT     {m:6} {p}")
for m, p in missed:
    print(f"  MISSED  {m:6} {p}")
print(f"\n{len(covered)}/{len(new)} exercised")
sys.exit(1 if missed else 0)
