"""How long one Airtable send may legitimately take — and nothing else.

**Why this is its own module, outside both packages.** The outbox lease and the
client's retry budget are *one decision*: a lease shorter than the worst case a
single send can take means a still-working worker has its entry stolen, two
workers send the same phase, and the loser can record a stale outcome over the
winner's. So both sides must read the same numbers.

They used to, by `app.sync.outbox` importing `app.airtable.client`. That single
import is what made the outbox — pure persistence, no socket — transitively
depend on the HTTP transport, and it is why `report-api` could not enqueue a row
without pulling the Airtable client into the request path. The dependency was
one function and three integers.

So the numbers live here: no imports, no transport, nothing to fail. `client.py`
and `sync/outbox.py` both read from this module, and neither reads from the
other.
"""

# Between requests, so a burst of sends does not trip Airtable's rate limit.
MIN_REQUEST_INTERVAL = 0.2

# Socket timeout for one attempt.
DEFAULT_TIMEOUT = 30

# Attempts per request, including the first.
MAX_REQUEST_ATTEMPTS = 5


def request_budget_seconds(max_attempts=None, timeout=DEFAULT_TIMEOUT):
    """Worst-case wall clock for one `request()` call, in seconds.

    The outbox lease is computed from this, so that "how long may a worker hold
    an entry" and "how long may one send legitimately take" are a single
    decision rather than two numbers that quietly disagree. With the defaults
    this is ~168 s, which is why a 120 s lease was wrong.

    Counts every attempt's socket timeout, every inter-attempt backoff at its
    ceiling including maximum jitter, and the inter-request throttle.
    """
    max_attempts = MAX_REQUEST_ATTEMPTS if max_attempts is None else max_attempts
    sockets = timeout * max_attempts
    backoff = sum(min(2 ** (a - 1), 30) + 0.5 for a in range(1, max_attempts))
    throttle = MIN_REQUEST_INTERVAL * max_attempts
    return sockets + backoff + throttle
