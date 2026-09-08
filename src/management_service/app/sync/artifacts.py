"""Photograph previews for Airtable: generate, hash, name, and size-check.

Contract §6, verbatim on the points that constrain this module:

    LabOS retains original photos and generates a persisted JPEG preview under
    the direct-upload limit for Airtable. Preview ID is persistent, with full
    content hash and deterministic filename.

So three properties, and each rules out a shortcut:

* **A preview, not the original.** Originals are certification evidence at full
  resolution; Airtable gets a downscaled JPEG. Sending the original would put
  the evidence itself behind an upload size limit.
* **A deterministic filename from the content hash.** Regenerating a preview for
  the same photograph produces the same name, so a retry after an ambiguous
  upload can recognise its own file on the record instead of appending a second
  copy. A timestamp or a uuid in the name would make that impossible.
* **Under the limit before the request is made.** Airtable's direct upload caps
  a single attachment at 5 MB. Discovering that from a 413 wastes the operator's
  time and leaves an ambiguous outcome; the cap is enforced here.

`Pillow` is added for this. It is the standard tool, the alternative is shipping
originals or hand-rolling JPEG resampling, and `reportlab` already puts image
handling in the dependency set.
"""

import hashlib
import io
import logging
import os

log = logging.getLogger("app.sync.artifacts")

# Airtable's cap for one attachment on the direct-upload endpoint.
AIRTABLE_ATTACHMENT_LIMIT = 5 * 1024 * 1024

# Our own target, well under it. A lab photograph at this size is legible for
# review, and the margin means a preview never fails the limit for a reason we
# would have to explain after the fact.
PREVIEW_MAX_BYTES = 2 * 1024 * 1024
PREVIEW_MAX_EDGE = 2048
PREVIEW_QUALITIES = (85, 75, 65, 55, 45)

CONTENT_TYPE = "image/jpeg"


class PreviewError(Exception):
    """The original could not be turned into a sendable preview."""


def content_hash(data):
    """Full SHA-256 of the bytes. §6 asks for the full hash, not a prefix."""
    return hashlib.sha256(data).hexdigest()


def preview_filename(photo_id, digest):
    """Deterministic, and carries the identity a reconciliation needs.

    `labos-<photo id>-<first 16 of the hash>.jpg`. The photo id makes it
    readable to a human scanning the record; the hash makes it *verifiable* —
    a reconciliation can tell our own re-upload apart from a different file
    someone attached by hand.
    """
    return f"labos-{photo_id}-{digest[:16]}.jpg"


def build_preview(path, photo_id):
    """Read an original and return `(bytes, filename, content_hash)`.

    Raises `PreviewError` when the file is missing or is not an image LabOS can
    downscale — both of which must surface rather than being sent as-is, since
    Airtable would reject the result and leave the outcome ambiguous.
    """
    if not path or not os.path.exists(path):
        raise PreviewError(
            f"the original is not readable at {path!r}. The sync worker needs "
            "the same uploads volume the API writes to — see compose.yaml.")
    try:
        from PIL import Image
    except ImportError as exc:                                # pragma: no cover
        raise PreviewError(
            "Pillow is not installed, so no preview can be generated. "
            "LabOS does not send originals: they are full-resolution "
            "certification evidence and would fail the attachment limit."
        ) from exc

    try:
        with Image.open(path) as img:
            img.load()
            # Flatten to RGB: a PNG with alpha cannot be saved as JPEG, and a
            # palette image loses colours silently if converted late.
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE))
            for quality in PREVIEW_QUALITIES:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                data = buf.getvalue()
                if len(data) <= PREVIEW_MAX_BYTES:
                    break
            else:
                raise PreviewError(
                    f"could not bring {path!r} under {PREVIEW_MAX_BYTES} bytes "
                    f"at quality {PREVIEW_QUALITIES[-1]}")
    except PreviewError:
        raise
    except Exception as exc:                                  # noqa: BLE001
        raise PreviewError(f"{path!r} is not an image we can downscale: "
                           f"{type(exc).__name__}: {exc}") from exc

    digest = content_hash(data)
    return data, preview_filename(photo_id, digest), digest


def find_existing_attachment(attachments, filename):
    """Our own preview among a record's attachments, or None.

    Matched on the deterministic filename, which is why that name carries the
    content hash: this is the check that makes an ambiguous upload safe to
    resolve without appending a duplicate. §6 — "reconcile remote attachments
    and wait for any outstanding request to settle" rather than blindly retrying.
    """
    for att in attachments or ():
        if att.get("filename") == filename:
            return att
    return None
