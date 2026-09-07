"""`report-api` must not depend on Airtable. Structurally, not by convention.

Delivery plan §4.6. The rule that keeps standalone and synced from forking into
two systems is that the sync service never writes domain tables, and the API
never calls Airtable. Creating a project goes through the same route whether the
values were typed by an operator or pre-filled from the local mirror.

Written as a test because the failure mode is silent and gradual: one convenient
import inside a route handler, and an Airtable outage starts returning 500s to an
operator standing at a rig. The product owner's rule — "an Airtable connection
problem must not stop testing" — is only true while this passes.
"""

import unittest


class ReportApiIsolation(unittest.TestCase):

    def test_main_imports_no_airtable_or_sync_module(self):
        import sys
        for mod in [m for m in sys.modules
                    if m.startswith(("app.airtable", "app.sync"))]:
            del sys.modules[mod]

        import app.main  # noqa: F401

        leaked = sorted(m for m in sys.modules
                        if m.startswith(("app.airtable", "app.sync")))
        self.assertEqual(
            leaked, [],
            "report-api imported "
            f"{leaked} — it must never call Airtable. Results reach Airtable "
            "through sync_outbox, which the separate worker container drains.")

    def test_no_source_file_in_the_api_path_references_the_client(self):
        """A deferred import inside a handler would pass the check above."""
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "app"
        offenders = []
        for path in list((root / "data").rglob("*.py")) + [root / "main.py"]:
            text = path.read_text(encoding="utf-8")
            # Strip comments so prose explaining the rule does not trip it.
            code = "\n".join(l.split("#")[0] for l in text.splitlines())
            if re.search(r"\b(from|import)\s+.*\b(airtable|sync)\b", code):
                offenders.append(path.name)
        self.assertEqual(offenders, [],
                         f"{offenders} reference the Airtable/sync layer from the "
                         "request path")


if __name__ == "__main__":
    unittest.main()
