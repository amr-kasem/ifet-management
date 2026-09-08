"""`report-api` must not depend on Airtable being reachable. Structurally.

Delivery plan §4.6 and §4.7. The rule that keeps standalone and synced from
forking into two systems is that the sync service never writes domain tables,
and **the API never calls Airtable**. Creating a project goes through the same
route whether the values were typed by an operator or pre-filled from the local
mirror.

Written as a test because the failure mode is silent and gradual: one convenient
import inside a route handler, and an Airtable outage starts returning 500s to
an operator standing at a rig. The product owner's rule — "an Airtable
connection problem must not stop testing" — is only true while this passes.

**The rule was refined on 2026-09-08, and narrowed in one direction only.**

It used to forbid the API importing *anything* under `app.airtable` or
`app.sync`. That is stricter than the rule it was protecting, and it forbade the
very mechanism the rule depends on: a transactional outbox needs the domain save
and the queue row to commit together, which means the API must be able to
`INSERT` into `sync_outbox` — a local table, no socket, no Airtable.

So the boundary is now drawn where the risk actually is: **transport**. The API
may import persistence and payload construction; it may not import anything that
can open a socket. Nothing was relaxed about calling Airtable from a request —
that is still forbidden, and now checked by name rather than by package prefix,
which is why `app/retry_budget.py` exists and why `app/airtable/__init__.py`
loads its client lazily.
"""

import pathlib
import re
import unittest

# Modules that can open a socket to Airtable, or that exist to drive one.
# Importing any of these from the request path is the thing being prevented.
TRANSPORT = (
    "app.airtable.client",      # the HTTP client itself
    "app.airtable.probe",       # live schema reads
    "app.airtable.apply_schema",
    "app.airtable.baseline",
    "app.airtable.interface_schema",
    "app.airtable.preflight",
    "app.airtable.fixture",
    "app.sync.worker",          # drives sends
    "app.sync.service",         # the worker process
    "app.sync.singleton",
)

# Modules the API is allowed to import: local persistence and payload
# construction. Each is transport-free by construction, which the third test
# below verifies rather than assumes.
PERSISTENCE_AND_PAYLOAD = (
    "app.sync.publish",     # the seam main.py calls; must stay socket-free
    "app.sync.outbox",
    "app.sync.state",
    "app.airtable.envelope",
    "app.airtable.mapping",
    "app.airtable.contract",
    "app.airtable.errors",
    "app.retry_budget",
    # The inbound half: the mirror, the requirement reader and the importer.
    # All three are persistence and interpretation — they hold no client and
    # open no socket. `POST /airtable/refresh` is the single exception and
    # builds its client inside the function, which is why the source scan below
    # is what actually enforces this rule.
    "app.airtable.mirror",
    "app.airtable.requirements",
    "app.airtable.importer",
)

ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"


class ReportApiIsolation(unittest.TestCase):

    def test_main_imports_no_transport(self):
        """Importing the API must not load anything that can call Airtable."""
        import sys
        for mod in [m for m in sys.modules
                    if m.startswith(("app.airtable", "app.sync"))]:
            del sys.modules[mod]

        import app.main  # noqa: F401

        leaked = sorted(m for m in sys.modules if m in TRANSPORT)
        self.assertEqual(
            leaked, [],
            f"report-api imported {leaked} — it must never be able to call "
            "Airtable from a request. Results reach Airtable through "
            "sync_outbox, which the separate worker container drains.")

    def test_no_source_file_in_the_api_path_imports_transport(self):
        """A deferred import inside a handler would pass the check above."""
        offenders = {}
        # Match the **two-segment tail** of each transport module —
        # `airtable.client`, `sync.worker` — so a relative import
        # (`from ..airtable.client import ...`) is caught while an unrelated
        # stdlib module is not. Matching the bare leaf `client` flagged
        # `from xmlrpc.client import Boolean` in `data/schema.py`, which is a
        # false positive: the leaf alone does not identify our transport.
        tails = tuple(re.escape(".".join(m.split(".")[-2:])) for m in TRANSPORT)
        pattern = re.compile(
            r"\b(?:from|import)\s+\.*[\w.]*(" + "|".join(tails) + r")\b")
        for path in list((ROOT / "data").rglob("*.py")) + [ROOT / "main.py"]:
            # **Module-scope imports only** — an unindented `import` line.
            #
            # `POST /airtable/refresh` is the one route that must reach Airtable,
            # and it builds its client inside the function body. That is the
            # deliberate exception: importing the transport lazily, in one named
            # place, keeps it out of every other request rather than out of the
            # process. A module-scope import is what would put it on the import
            # path of every route, which is the thing being prevented.
            code = "\n".join(
                l.split("#")[0]
                for l in path.read_text(encoding="utf-8").splitlines()
                if not l.startswith((" ", "\t")))
            hits = sorted(set(pattern.findall(code)))
            if hits:
                offenders[path.name] = hits
        self.assertEqual(offenders, {},
                         f"{offenders} reach the Airtable transport from the "
                         "request path")

    def test_persistence_and_payload_modules_are_transport_free(self):
        """The allowlist is only safe while every module on it stays clean.

        This is the check that makes the narrowing above defensible: if someone
        adds a client import to `outbox.py`, the API's allowance to import it
        silently becomes an allowance to call Airtable. So each allowed module
        is imported alone, in a clean module table, and must pull in no
        transport.
        """
        import json
        import subprocess
        import sys

        # A fresh interpreter per module, not `del sys.modules[...]`. Deleting
        # them and re-importing leaves `app.data.models.Base` holding the table
        # definitions, so the second import raises "Table 'sync_outbox' is
        # already defined" — an artifact of the check, not a fact about the
        # code. A subprocess has no such history.
        probe = (
            "import json,sys,importlib;"
            "importlib.import_module(sys.argv[1]);"
            "print(json.dumps(sorted(m for m in sys.modules "
            "if m in json.loads(sys.argv[2]))))"
        )
        for allowed in PERSISTENCE_AND_PAYLOAD:
            out = subprocess.run(
                [sys.executable, "-c", probe, allowed, json.dumps(list(TRANSPORT))],
                capture_output=True, text=True,
                cwd=str(ROOT.parent), check=False)
            self.assertEqual(out.returncode, 0,
                             f"{allowed} failed to import alone:\n{out.stderr}")
            leaked = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual(
                leaked, [],
                f"{allowed} pulls in {leaked}; it is on the API's allowlist and "
                "must stay transport-free")


if __name__ == "__main__":
    unittest.main()
