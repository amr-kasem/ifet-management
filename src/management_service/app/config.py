"""Server-side configuration for the Airtable integration (P0 / Ref 42).

Every value comes from the environment, which compose populates from the
gitignored `.env` (see `.env.example`). Nothing here may ever be written to a
file the UI serves — `deployment/config/config.json` and
`src/ifet_ui_react/config.json` go to the browser.

Import this rather than calling os.getenv directly, so the token has exactly one
entry point into the process and redaction is not optional.
"""

import os

_TRUE = {"1", "true", "yes", "on"}


def _split_list(raw: str) -> tuple:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


class AirtableSettings:
    """Airtable connection settings, read once at import time.

    Deliberately tolerant of a missing token: the stack must start before the
    Airtable team issues one. `is_configured` is how callers find out.
    """

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.token = env.get("AIRTABLE_TOKEN", "").strip()
        self.base_id = env.get("AIRTABLE_BASE_ID", "").strip()
        self.results_table = env.get("AIRTABLE_RESULTS_TABLE", "LabOS Raw Test Results").strip()
        self.write_allowlist = _split_list(
            env.get("AIRTABLE_WRITE_ALLOWLIST", self.results_table)
        )
        self.sync_enabled = env.get("AIRTABLE_SYNC_ENABLED", "false").strip().lower() in _TRUE
        self.public_origin = env.get("LABOS_PUBLIC_ORIGIN", "").strip().rstrip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.base_id and self.results_table)

    @property
    def should_sync(self) -> bool:
        """Sync runs only when explicitly enabled AND fully configured.

        Two separate switches on purpose: the flag is the dark-launch control,
        and `is_configured` guards against a half-filled .env silently enabling
        writes.
        """
        return self.sync_enabled and self.is_configured

    def missing(self) -> tuple:
        """Which required settings are absent — for a startup log line."""
        return tuple(
            name
            for name, value in (
                ("AIRTABLE_TOKEN", self.token),
                ("AIRTABLE_BASE_ID", self.base_id),
                ("AIRTABLE_RESULTS_TABLE", self.results_table),
            )
            if not value
        )

    def assert_writable(self, table: str) -> None:
        """Refuse any table but the allowlisted one.

        An Airtable personal access token is scoped per BASE, not per table, so
        the token itself cannot stop us from writing the Airtable team's
        read-only tables. This check is what actually enforces that boundary.
        """
        if table not in self.write_allowlist:
            raise PermissionError(
                f"refusing to write Airtable table {table!r}: not in "
                f"AIRTABLE_WRITE_ALLOWLIST {list(self.write_allowlist)}"
            )

    def report_url(self, path: str) -> str:
        """Absolute URL for a link written into Airtable.

        Raises rather than emitting a localhost link, which would resolve to
        nothing for every Airtable user (internal-plan gap B).
        """
        if not self.public_origin:
            raise RuntimeError(
                "LABOS_PUBLIC_ORIGIN is not set; refusing to write a "
                "non-resolvable report/photo link into Airtable"
            )
        return f"{self.public_origin}/{path.lstrip('/')}"

    def redacted(self) -> dict:
        """Safe to log. The token is never returned, in any form."""
        return {
            "base_id": self.base_id,
            "results_table": self.results_table,
            "write_allowlist": list(self.write_allowlist),
            "sync_enabled": self.sync_enabled,
            "token_present": bool(self.token),
            "public_origin": self.public_origin or None,
        }

    def __repr__(self) -> str:  # keeps the token out of tracebacks and logs
        return f"AirtableSettings({self.redacted()})"

    __str__ = __repr__


airtable_settings = AirtableSettings()


if __name__ == "__main__":
    import sys

    s = airtable_settings
    print(f"airtable: {s.redacted()}")
    if s.should_sync:
        print("state: sync ENABLED")
    elif s.is_configured:
        print("state: configured, sync disabled (dark launch)")
    else:
        print(f"state: not configured (missing: {', '.join(s.missing()) or 'none'})")
    # Not an error: an unconfigured Airtable integration is the expected state
    # until the rotated token arrives.
    sys.exit(0)
