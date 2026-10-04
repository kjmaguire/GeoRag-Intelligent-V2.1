"""The ingest-side view of the upload ceiling (``GEORAG_MAX_UPLOAD_BYTES``).

Laravel (``Uploads::maxBytes`` in app/Support/Uploads.php) owns the number: every file that
reaches ingestion was admitted under it, and it defaults to 512 MiB. The
workers used to carry their own hard-coded 2 GiB (``ingest_pdf._MAX_PDF_BYTES``,
``tiff_to_pdf.MAX_TIFF_BYTES``), "mirrored" from a ceiling that had already
been lowered, so the preflight check could never fire for anything the web tier
let through and the 2 GiB figure in the error text was a fiction. This reads
the same variable with the same rule (a positive integer, else the default),
so the two cannot drift: raise ``GEORAG_MAX_UPLOAD_BYTES`` on every service
that sets it and the ingest ceiling moves with it.

Read once at import by its two consumers (as the constants they replace were),
because a worker's ceiling should not change under a running parse.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("georag.ingest.upload_limits")

ENV_NAME = "GEORAG_MAX_UPLOAD_BYTES"

#: Mirrors ``Uploads::DEFAULT_MAX_BYTES`` in app/Support/Uploads.php.
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def max_upload_bytes() -> int:
    """The per-file ceiling in bytes: ``GEORAG_MAX_UPLOAD_BYTES`` or 512 MiB."""
    raw = (os.environ.get(ENV_NAME) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        # Unset or not an integer: Laravel's Uploads rule falls back the same way.
        log.debug("%s=%r is not an integer; using the 512 MiB default", ENV_NAME, raw, exc_info=True)
        return DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


def human_bytes(size: int) -> str:
    """``536870912`` -> ``"512 MB"``, ``2147483648`` -> ``"2 GB"`` (for messages)."""
    mb = size / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:g} GB"
    return f"{mb:g} MB"


__all__ = ["DEFAULT_MAX_UPLOAD_BYTES", "ENV_NAME", "human_bytes", "max_upload_bytes"]
