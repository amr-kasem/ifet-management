"""Airtable REST client for LabOS.

Built against write contract v0.3 (`ifet-firmware/docs/`). Three things here are
load-bearing rather than incidental:

1.  **The write allowlist is enforced before the socket opens.** Airtable PAT
    scopes are per *base*, not per table, so the testing token can write every
    table in the base — including the four the Airtable team marked READ only.
    Nothing on their side stops a stray write. This client is the enforcement.

2.  **Retry behaviour is decided by the error type, not by the caller** (§8).

3.  **Standard library only.** `report-api`'s `app/` is bind-mounted into the
    running container, so a pure-stdlib module is live without an image
    rebuild — and on this project a rebuild of a production container is a
    scheduled event, not a convenience. Adding `requests` here would have made
    the schema probe undeployable without one.

The token is never logged, never repr'd, and never included in an exception.
"""

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from ..config import airtable_settings
from .errors import (
    AirtableError,
    AirtableTransportError,
    AirtableWriteForbidden,
    classify,
)

API_ROOT = "https://api.airtable.com/v0"

# Airtable allows 5 requests/second per base. 0.2s spacing keeps us at the limit
# without a token bucket; the sync worker is serialized anyway (§8).
MIN_REQUEST_INTERVAL = 0.2

# Airtable's hard limit on records per create/update request (§2).
MAX_BATCH = 10

DEFAULT_TIMEOUT = 30


class AirtableClient:
    """Thin, explicit Airtable client. One instance per process is plenty."""

    def __init__(self, settings=None, transport=None, sleep=time.sleep,
                 monotonic=time.monotonic, timeout=DEFAULT_TIMEOUT):
        self.settings = settings or airtable_settings
        # `transport` exists so every retry path, rate-limit path and error
        # class can be tested without a network. Production leaves it None.
        self._transport = transport or self._urllib_transport
        self._sleep = sleep
        self._monotonic = monotonic
        self._timeout = timeout
        self._last_request_at = None

    # ------------------------------------------------------------- transport

    def _urllib_transport(self, method, url, headers, body):
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:            # a real HTTP response
            return exc.code, exc.read()
        except urllib.error.URLError as exc:             # never reached Airtable
            raise AirtableTransportError(f"transport failure: {exc.reason}", url=url)
        except TimeoutError:
            raise AirtableTransportError("request timed out", url=url)

    def _headers(self):
        if not self.settings.token:
            raise AirtableWriteForbidden(
                "no AIRTABLE_TOKEN configured — the Airtable team has not "
                "delivered the testing PAT yet (contract §10.10)"
            )
        return {
            "Authorization": f"Bearer {self.settings.token}",
            "Content-Type": "application/json",
        }

    def _throttle(self):
        if self._last_request_at is None:
            return
        elapsed = self._monotonic() - self._last_request_at
        if elapsed < MIN_REQUEST_INTERVAL:
            self._sleep(MIN_REQUEST_INTERVAL - elapsed)

    # ----------------------------------------------------------- the request

    def request(self, method, url, payload=None, max_attempts=5):
        """One Airtable call, with throttling and typed retries.

        Retries only what §8 says is retryable: 429, 5xx, and transport
        failures. A 422 raises immediately — retrying a malformed payload is
        just a slower failure — and 401/403 raises immediately so a bad token
        is not hammered.
        """
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = self._headers()
        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            try:
                status, raw = self._transport(method, url, headers, body)
                self._last_request_at = self._monotonic()
            except AirtableTransportError as exc:
                if attempt >= max_attempts:
                    raise
                self._backoff(attempt, exc)
                continue

            if 200 <= status < 300:
                return json.loads(raw.decode("utf-8")) if raw else {}

            error = classify(status, self._safe_body(raw), url)
            if not error.retryable or attempt >= max_attempts:
                raise error
            self._backoff(attempt, error, raw)

    def _backoff(self, attempt, error, raw=None):
        """Exponential backoff with jitter; honours Retry-After when present."""
        delay = min(2 ** (attempt - 1), 30) + random.uniform(0, 0.5)
        self._sleep(delay)

    @staticmethod
    def _safe_body(raw):
        """Decode an error body for logging. Never echoes a request header."""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return raw[:400].decode("utf-8", "replace")

    # --------------------------------------------------------------- reading

    def get_base_schema(self, base_id=None):
        """GET /v0/meta/bases/{base}/tables — needs `schema.bases:read`.

        This single call is what closes contract open items 1, 2, 5 and 16 and
        verifies every table ID in §0.1. See `app.airtable.probe`.
        """
        base = base_id or self.settings.base_id
        return self.request("GET", f"{API_ROOT}/meta/bases/{base}/tables")

    def list_records(self, table_id, page_size=100, view=None, fields=None,
                     formula=None, offset=None, by_field_id=False):
        """One page of records. Read-only; no allowlist check needed."""
        params = {"pageSize": min(page_size, 100)}
        if view:
            params["view"] = view
        if formula:
            params["filterByFormula"] = formula
        if offset:
            params["offset"] = offset
        if by_field_id:
            # Contract §9: binding to field IDs makes an Airtable-side rename a
            # non-event, and a removal fail loudly instead of silently.
            params["returnFieldsByFieldId"] = "true"
        query = urllib.parse.urlencode(params, doseq=True)
        if fields:
            query += "&" + "&".join(
                "fields%5B%5D=" + urllib.parse.quote(f) for f in fields
            )
        url = f"{API_ROOT}/{self.settings.base_id}/{table_id}?{query}"
        return self.request("GET", url)

    def iter_records(self, table_id, max_pages=100, **kwargs):
        """Follow Airtable's `offset` pagination. Yields records, not pages."""
        offset = None
        for _ in range(max_pages):
            page = self.list_records(table_id, offset=offset, **kwargs)
            for record in page.get("records", []):
                yield record
            offset = page.get("offset")
            if not offset:
                return

    def get_record(self, table_id, record_id):
        url = f"{API_ROOT}/{self.settings.base_id}/{table_id}/{record_id}"
        return self.request("GET", url)

    # --------------------------------------------------------------- writing

    def _assert_may_write(self, table_id):
        """Every write passes through here. No exceptions, no bypass."""
        try:
            self.settings.assert_writable(table_id)
            self.settings.assert_production_write_allowed()
        except PermissionError as exc:
            raise AirtableWriteForbidden(str(exc))

    def upsert_records(self, table_id, records, merge_on=("LabOS Attempt ID",),
                       typecast=False):
        """PATCH with performUpsert — the only write LabOS makes (contract §2).

        Upsert rather than create, because the merge key is minted by LabOS
        before the first send. A request that times out after Airtable committed
        it is then safe to retry: the retry merges onto the same row instead of
        producing a duplicate attempt.
        """
        self._assert_may_write(table_id)
        if not records:
            return {"records": []}
        if len(records) > MAX_BATCH:
            raise ValueError(
                f"Airtable accepts at most {MAX_BATCH} records per request; "
                f"got {len(records)}. Batch upstream."
            )
        payload = {
            "performUpsert": {"fieldsToMergeOn": list(merge_on)},
            "records": [{"fields": r} for r in records],
        }
        if typecast:
            payload["typecast"] = True
        url = f"{API_ROOT}/{self.settings.base_id}/{table_id}"
        return self.request("PATCH", url, payload)

    def create_records(self, table_id, records, typecast=False):
        """Plain create. Present for probe/diagnostic use — the sync worker
        upserts, so that a retry can never duplicate an attempt."""
        self._assert_may_write(table_id)
        if len(records) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} records per request")
        payload = {"records": [{"fields": r} for r in records]}
        if typecast:
            payload["typecast"] = True
        url = f"{API_ROOT}/{self.settings.base_id}/{table_id}"
        return self.request("POST", url, payload)


__all__ = ["AirtableClient", "AirtableError", "API_ROOT", "MAX_BATCH"]
