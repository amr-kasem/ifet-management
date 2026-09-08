"""LabOS ↔ Airtable integration (Epic IFET-32).

Contract: `ifet-firmware/docs/labos-airtable/contract/write-contract-v0.3.md`.
Import-safe and side-effect-free: nothing here opens a socket at import time,
because `report-api`'s `app/` is bind-mounted into a production container.
"""

# `AirtableClient` is **not** imported here. It is the HTTP transport, and an
# eager import made `from app.airtable import envelope` — pure payload
# construction with no socket — pull the transport in as a side effect. That is
# what stopped `report-api` from building an outbox payload without depending on
# Airtable being reachable.
#
# PEP 562 module `__getattr__` keeps `from app.airtable import AirtableClient`
# working for the worker, while importing nothing until someone actually asks
# for it. Everything else below is transport-free: `envelope`, `errors` and
# `contract` have no imports outside this package and open no sockets.
from .envelope import (
    EnvelopeError,
    build,
    build_start,
    build_terminal,
    options_from_snapshot,
)
from .errors import (
    AirtableAuthError,
    AirtableError,
    AirtableRateLimited,
    AirtableServerError,
    AirtableTransportError,
    AirtableValidationError,
    AirtableWriteForbidden,
)

def __getattr__(name):
    """Lazily expose the transport, so importing it is a choice not a default."""
    if name == "AirtableClient":
        from .client import AirtableClient
        return AirtableClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AirtableClient",
    "build",
    "build_start",
    "build_terminal",
    "options_from_snapshot",
    "EnvelopeError",
    "AirtableError",
    "AirtableAuthError",
    "AirtableRateLimited",
    "AirtableServerError",
    "AirtableTransportError",
    "AirtableValidationError",
    "AirtableWriteForbidden",
]
