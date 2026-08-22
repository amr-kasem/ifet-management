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

# ---------------------------------------------------------------- Airtable IDs
# From the Airtable team's "API Integration Guide v2" (2026-08-17), recorded in
# ifet-firmware/docs/labos-airtable-write-contract-v0.3.md §0.1.
#
# These are DEFAULTS and cross-checks, not gospel: v2 itself says to confirm the
# live IDs against the schema endpoint before any production cutover. That is
# what `app.airtable.probe` does — it diffs these against the live base.

BASE_TESTING = "app4oXS3Kd5IKWgJ7"      # LabOS Testing Base — build here
BASE_PRODUCTION = "app0OCunbmuXl7Hc9"   # IFET Airtable Base — gated

# The single writable surface. Everything else in the base is read-only to LabOS
# by agreement, and by the allowlist below in fact.
TABLE_RAW_DATA = "tblnc9SsbXU0C0FWh"

# Read-only hierarchy tables (v2 §2). Named so a misdirected write reports
# something more useful than a bare table ID.
TABLE_NAMES = {
    "tblLYcRC7q6Srjfk3": "IFET Projects",
    "tblcrGv0WJn6FTTGO": "Mock-Ups/Specimens",
    "tblutO1Q8TNC4BLk0": "Tests Protocols",
    "tblqpvuJlSdkeS9PS": "Protocol Sections",
    TABLE_RAW_DATA: "LabOS Raw Data Table",
    "tblVUvcSPAoneG26W": "Walls & Positions",
    "tblYjF1AApzmRDMrY": "Wall Scheduling/Reservation",
    "tbly6A3GB1GHdGocq": "Wall Positions Calendar",
    "tbl0f2YxS3FHJ1dTD": "Back Charges",
}

READ_TABLES = (
    "tblLYcRC7q6Srjfk3",
    "tblcrGv0WJn6FTTGO",
    "tblutO1Q8TNC4BLk0",
    "tblqpvuJlSdkeS9PS",
)


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
        self.base_id = env.get("AIRTABLE_BASE_ID", BASE_TESTING).strip()
        # Table IDs, not names. A table rename on the Airtable side would
        # silently defeat a name-based allowlist while leaving it looking
        # correct; `tbl…` IDs are stable across renames. v2 published them, so
        # there is no longer any reason to address tables by name.
        self.results_table = env.get("AIRTABLE_RESULTS_TABLE", TABLE_RAW_DATA).strip()
        self.write_allowlist = _split_list(
            env.get("AIRTABLE_WRITE_ALLOWLIST", self.results_table)
        )
        self.sync_enabled = env.get("AIRTABLE_SYNC_ENABLED", "false").strip().lower() in _TRUE
        self.allow_production_write = (
            env.get("AIRTABLE_ALLOW_PRODUCTION_WRITE", "false").strip().lower() in _TRUE
        )
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

    @property
    def environment(self) -> str:
        """Which Airtable base this process is pointed at."""
        if self.base_id == BASE_PRODUCTION:
            return "production"
        if self.base_id == BASE_TESTING:
            return "testing"
        return "unknown"

    @property
    def is_production_base(self) -> bool:
        return self.base_id == BASE_PRODUCTION

    def assert_writable(self, table: str) -> None:
        """Refuse any table but the allowlisted one.

        An Airtable personal access token is scoped per BASE, not per table, so
        the token itself cannot stop us from writing the Airtable team's
        read-only tables. This check is what actually enforces that boundary.
        """
        if table not in self.write_allowlist:
            label = TABLE_NAMES.get(table)
            known = f" ({label})" if label else ""
            hint = ""
            if table in READ_TABLES:
                hint = (
                    " — this is one of the Airtable team's READ-ONLY hierarchy "
                    "tables; LabOS must never write it"
                )
            raise PermissionError(
                f"refusing to write Airtable table {table!r}{known}: not in "
                f"AIRTABLE_WRITE_ALLOWLIST {list(self.write_allowlist)}{hint}"
            )

    def assert_production_write_allowed(self) -> None:
        """Guard the production base behind an explicit, separate opt-in.

        v2 §5 is emphatic that production write-back must not begin until the
        Airtable team enables their processing automation, and §7 keeps that
        approval on their side. A correct token plus a correct base ID is
        therefore NOT sufficient authority to write production — so pointing
        AIRTABLE_BASE_ID at the production base is deliberately not enough on
        its own. AIRTABLE_ALLOW_PRODUCTION_WRITE has to be set as well, by a
        human who knows the automation has been switched on.
        """
        if self.is_production_base and not self.allow_production_write:
            raise PermissionError(
                "refusing to write the Airtable PRODUCTION base "
                f"{self.base_id!r}: set AIRTABLE_ALLOW_PRODUCTION_WRITE=true "
                "only after the Airtable team has confirmed their processing "
                "automation is enabled (guide v2 §5, §7)"
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
            "environment": self.environment,
            "allow_production_write": self.allow_production_write,
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
