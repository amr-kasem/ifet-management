"""Client tests — offline. No token, no socket, no Airtable.

The behaviours asserted here are the ones that keep a bug from becoming an
incident: the write allowlist, the production gate, the retry classes, and the
guarantee that the token never appears in a log line or a traceback.
"""

import json
import unittest

from app.airtable.client import AirtableClient, MAX_BATCH
from app.airtable.errors import (
    AirtableAuthError,
    AirtableRateLimited,
    AirtableServerError,
    AirtableTransportError,
    AirtableValidationError,
    AirtableWriteForbidden,
)
from app.config import BASE_PRODUCTION, BASE_TESTING, TABLE_RAW_DATA, AirtableSettings

READ_TABLE = "tblqpvuJlSdkeS9PS"      # Protocol Sections — read-only
TOKEN = "patFAKE1234567.abcdefghijklmnopqrstuvwxyz0123456789"


def settings(**over):
    env = {"AIRTABLE_TOKEN": TOKEN, "AIRTABLE_BASE_ID": BASE_TESTING}
    env.update(over)
    return AirtableSettings(env)


class Transport:
    """Scripted responses; records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": json.loads(body) if body else None})
        item = self.responses.pop(0) if self.responses else (200, b"{}")
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return status, json.dumps(payload).encode() if isinstance(payload, dict) else payload


def client(transport=None, **over):
    return AirtableClient(settings(**over), transport=transport or Transport(),
                          sleep=lambda _s: None)


class WriteAllowlist(unittest.TestCase):
    def test_refuses_read_only_hierarchy_table_before_any_request(self):
        t = Transport()
        c = client(t)
        with self.assertRaises(AirtableWriteForbidden) as ctx:
            c.upsert_records(READ_TABLE, [{"LabOS Attempt ID": "a1"}])
        self.assertIn("READ-ONLY", str(ctx.exception))
        self.assertEqual(t.calls, [], "a forbidden write must not touch the network")

    def test_refuses_unknown_table(self):
        with self.assertRaises(AirtableWriteForbidden):
            client().upsert_records("tblSomethingElse", [{"x": 1}])

    def test_allows_the_one_writable_table(self):
        t = Transport((200, {"records": []}))
        client(t).upsert_records(TABLE_RAW_DATA, [{"LabOS Attempt ID": "a1"}])
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(t.calls[0]["method"], "PATCH")

    def test_create_is_also_gated(self):
        t = Transport()
        with self.assertRaises(AirtableWriteForbidden):
            client(t).create_records(READ_TABLE, [{"x": 1}])
        self.assertEqual(t.calls, [])

    def test_reads_are_not_gated(self):
        t = Transport((200, {"records": []}))
        client(t).list_records(READ_TABLE)
        self.assertEqual(len(t.calls), 1)


class ProductionGate(unittest.TestCase):
    def test_production_base_write_refused_without_explicit_optin(self):
        t = Transport()
        c = client(t, AIRTABLE_BASE_ID=BASE_PRODUCTION)
        with self.assertRaises(AirtableWriteForbidden) as ctx:
            c.upsert_records(TABLE_RAW_DATA, [{"LabOS Attempt ID": "a1"}])
        self.assertIn("AIRTABLE_ALLOW_PRODUCTION_WRITE", str(ctx.exception))
        self.assertEqual(t.calls, [])

    def test_production_write_allowed_once_opted_in(self):
        t = Transport((200, {"records": []}))
        c = client(t, AIRTABLE_BASE_ID=BASE_PRODUCTION,
                   AIRTABLE_ALLOW_PRODUCTION_WRITE="true")
        c.upsert_records(TABLE_RAW_DATA, [{"LabOS Attempt ID": "a1"}])
        self.assertEqual(len(t.calls), 1)

    def test_reading_production_needs_no_optin(self):
        t = Transport((200, {"records": []}))
        client(t, AIRTABLE_BASE_ID=BASE_PRODUCTION).list_records(READ_TABLE)
        self.assertEqual(len(t.calls), 1)


class RetryClasses(unittest.TestCase):
    def test_429_is_retried_then_succeeds(self):
        t = Transport((429, {"error": "rate"}), (200, {"records": [1]}))
        out = client(t).get_base_schema()
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(out, {"records": [1]})

    def test_422_raises_immediately_and_is_never_retried(self):
        t = Transport((422, {"error": {"type": "INVALID_MULTIPLE_CHOICE_OPTIONS"}}),
                      (200, {"records": []}))
        with self.assertRaises(AirtableValidationError):
            client(t).upsert_records(TABLE_RAW_DATA, [{"Test Result": "Passed"}])
        self.assertEqual(len(t.calls), 1, "retrying a malformed payload is a slower failure")

    def test_401_halts_and_is_never_retried(self):
        t = Transport((401, {"error": "unauthorized"}), (200, {}))
        with self.assertRaises(AirtableAuthError) as ctx:
            client(t).get_base_schema()
        self.assertEqual(len(t.calls), 1, "a bad token must not be hammered")
        self.assertTrue(ctx.exception.halt)

    def test_5xx_retried_to_the_attempt_limit(self):
        t = Transport(*[(503, {"e": 1})] * 5)
        with self.assertRaises(AirtableServerError):
            client(t).get_base_schema()
        self.assertEqual(len(t.calls), 5)

    def test_transport_failure_is_retried(self):
        t = Transport(AirtableTransportError("connection reset"), (200, {"ok": 1}))
        self.assertEqual(client(t).get_base_schema(), {"ok": 1})
        self.assertEqual(len(t.calls), 2)

    def test_retryable_flags_match_the_contract(self):
        self.assertTrue(AirtableRateLimited("x").retryable)
        self.assertTrue(AirtableServerError("x").retryable)
        self.assertTrue(AirtableTransportError("x").retryable)
        self.assertFalse(AirtableValidationError("x").retryable)
        self.assertFalse(AirtableAuthError("x").retryable)


class Payloads(unittest.TestCase):
    def test_upsert_uses_attempt_id_as_the_merge_key(self):
        t = Transport((200, {"records": []}))
        client(t).upsert_records(TABLE_RAW_DATA, [{"LabOS Attempt ID": "a1"}])
        body = t.calls[0]["body"]
        self.assertEqual(body["performUpsert"]["fieldsToMergeOn"], ["LabOS Attempt ID"])
        self.assertEqual(body["records"], [{"fields": {"LabOS Attempt ID": "a1"}}])

    def test_batch_limit_enforced(self):
        rows = [{"LabOS Attempt ID": f"a{i}"} for i in range(MAX_BATCH + 1)]
        with self.assertRaises(ValueError):
            client().upsert_records(TABLE_RAW_DATA, rows)

    def test_exactly_ten_is_allowed(self):
        t = Transport((200, {"records": []}))
        rows = [{"LabOS Attempt ID": f"a{i}"} for i in range(MAX_BATCH)]
        client(t).upsert_records(TABLE_RAW_DATA, rows)
        self.assertEqual(len(t.calls), 1)

    def test_empty_upsert_makes_no_request(self):
        t = Transport()
        self.assertEqual(client(t).upsert_records(TABLE_RAW_DATA, []), {"records": []})
        self.assertEqual(t.calls, [])

    def test_field_id_binding_is_requestable(self):
        t = Transport((200, {"records": []}))
        client(t).list_records(READ_TABLE, by_field_id=True)
        self.assertIn("returnFieldsByFieldId=true", t.calls[0]["url"])


class Throttling(unittest.TestCase):
    def test_requests_are_spaced_to_respect_5_per_second(self):
        slept, clock = [], [0.0]
        t = Transport((200, {}), (200, {}))
        c = AirtableClient(settings(), transport=t,
                           sleep=lambda s: slept.append(s),
                           monotonic=lambda: clock[0])
        c.get_base_schema()
        c.get_base_schema()
        self.assertTrue(slept and abs(slept[0] - 0.2) < 1e-9,
                        f"expected a ~0.2s gap, got {slept}")


class TokenHygiene(unittest.TestCase):
    def test_token_absent_from_settings_repr(self):
        self.assertNotIn("patFAKE", repr(settings()))
        self.assertNotIn(TOKEN, str(settings().redacted()))

    def test_token_absent_from_raised_errors(self):
        t = Transport((401, {"error": "unauthorized"}))
        try:
            client(t).get_base_schema()
        except AirtableAuthError as exc:
            self.assertNotIn("patFAKE", str(exc))
            self.assertNotIn(TOKEN, repr(exc))
        else:
            self.fail("expected AirtableAuthError")

    def test_missing_token_refuses_before_the_network(self):
        t = Transport()
        c = AirtableClient(AirtableSettings({"AIRTABLE_BASE_ID": BASE_TESTING}),
                           transport=t, sleep=lambda _s: None)
        with self.assertRaises(AirtableWriteForbidden):
            c.get_base_schema()
        self.assertEqual(t.calls, [])


if __name__ == "__main__":
    unittest.main()
