"""Airtable error taxonomy — the retry classes from write contract v0.3 §8.

The point of separate classes is that the *caller never decides* how to react to
an HTTP status. A 422 and a 503 are both "the write did not land", but retrying
one is correct and retrying the other is just a slower failure. Encoding that in
the type means the sync worker cannot get it wrong by omission.
"""


class AirtableError(Exception):
    """Base class. Carries status and a redacted body, never the token."""

    retryable = False

    def __init__(self, message, status=None, body=None, url=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.url = url

    def __str__(self):
        bits = [self.message]
        if self.status is not None:
            bits.append(f"HTTP {self.status}")
        if self.url:
            bits.append(self.url)
        if self.body:
            bits.append(str(self.body)[:400])
        return " · ".join(bits)


class AirtableAuthError(AirtableError):
    """401 / 403 — halt the worker and alert.

    Contract §8: a bad or revoked token must not be hammered. Retrying cannot
    fix authorization, and repeated 401s against Airtable look like an attack.
    """

    retryable = False
    halt = True


class AirtableValidationError(AirtableError):
    """422 — the payload is wrong. Do NOT retry.

    Unknown select option, bad field name, wrong type. The attempt is marked
    `Retry Required` and surfaced to the operator, because only a human or a
    contract change can fix it.
    """

    retryable = False
    halt = False


class AirtableRateLimited(AirtableError):
    """429 — back off and retry. Airtable allows 5 requests/second per base."""

    retryable = True
    halt = False


class AirtableServerError(AirtableError):
    """5xx — transient on their side. Retry with backoff."""

    retryable = True
    halt = False


class AirtableTransportError(AirtableError):
    """Socket/DNS/TLS failure — never reached Airtable, or the reply was lost.

    Retryable, and safe to retry precisely because every write is an upsert on
    a client-minted `LabOS Attempt ID` (contract §2): if the request actually
    did land before the connection dropped, the retry merges onto the same row
    instead of creating a second one.
    """

    retryable = True
    halt = False


class AirtableWriteForbidden(AirtableError):
    """A LabOS-side refusal, not an Airtable response.

    Raised before any network call when the target table is not allowlisted, or
    when the production base is targeted without explicit opt-in. Distinct from
    AirtableAuthError because nothing is wrong with the token — the guard did
    its job.
    """

    retryable = False
    halt = True


def classify(status, body=None, url=None):
    """Map an HTTP status to the right error class. Never returns for 2xx."""
    if status in (401, 403):
        return AirtableAuthError("Airtable rejected the token", status, body, url)
    if status == 422:
        return AirtableValidationError(
            "Airtable rejected the payload (unprocessable)", status, body, url
        )
    if status == 429:
        return AirtableRateLimited("Airtable rate limit", status, body, url)
    if 500 <= status < 600:
        return AirtableServerError("Airtable server error", status, body, url)
    return AirtableError(f"unexpected Airtable status {status}", status, body, url)
