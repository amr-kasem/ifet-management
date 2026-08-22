"""LabOS ↔ Airtable integration (Epic IFET-32).

Contract: `ifet-firmware/docs/labos-airtable-write-contract-v0.3.md`.
Import-safe and side-effect-free: nothing here opens a socket at import time,
because `report-api`'s `app/` is bind-mounted into a production container.
"""

from .client import AirtableClient
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
