"""Log-safe helpers for emitting user-query metadata without plaintext leakage.

P0 #3 — the audit log's `query_text` column is encrypted at rest (A4), but
every `logger.info("query='%.80s'", query)` call was bypassing that by
dumping the plaintext to Docker stdout → Loki. Anyone with `docker logs`
or Grafana/Loki access could read every customer's question.

Use `query_hash(q)` to emit a deterministic short hash — enough to
correlate a log line back to the encrypted audit row via
`QueryAuditLog::hashQueryText()` (which uses the same HMAC shape), but
reveals nothing about content.

The hash is HMAC-SHA256 keyed on FASTAPI_SERVICE_KEY so even an insider
with read access to the log files can't brute-force the plaintext from
a known-query dictionary without also knowing the secret.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any


def query_hash(query: str | None, key: str | None = None) -> str:
    """Short, deterministic, non-reversible fingerprint for a query.

    16 hex chars = 64 bits of collision space — enough to identify a
    specific query within the audit log but too short to reconstruct
    the plaintext via rainbow tables.

    When `key` is omitted we pull it from settings.FASTAPI_SERVICE_KEY
    (R13 guarantees ≥32 bytes) so the hash has HMAC properties, not
    just raw SHA-256. Fall through to plain SHA-256 in the (impossible
    at runtime) case the key isn't available.
    """
    if query is None:
        return "<none>"
    normalised = query.strip().lower()
    if not normalised:
        return "<empty>"

    if key is None:
        try:
            from app.config import settings  # noqa: PLC0415
            key = settings.FASTAPI_SERVICE_KEY
        except Exception:
            key = ""

    if key:
        return hmac.new(
            key.encode("utf-8"),
            normalised.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:16]
    # Defensive: should never land here if FASTAPI_SERVICE_KEY is set.
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]



def text_shape(text: str | None) -> str:
    """A content-free description of a string, for diagnostics.

    Answers "why did this input behave oddly" without answering "what did
    it say". Used where a raw excerpt would otherwise be logged: the
    failure modes that make people reach for the excerpt -- an empty
    sparse vector, a tokenizer producing nothing -- are properties of
    character classes and length, both of which are here.
    """
    if text is None:
        return "len=0 shape=<none>"
    if not text:
        return "len=0 shape=<empty>"

    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    digits = sum(1 for c in text if c.isdigit())
    spaces = sum(1 for c in text if c.isspace())
    non_ascii = sum(1 for c in text if not c.isascii())
    other = len(text) - ascii_letters - digits - spaces - non_ascii

    return (
        f"len={len(text)} alpha={ascii_letters} digit={digits} "
        f"space={spaces} nonascii={non_ascii} other={other}"
    )

def project_tag(project_id: Any) -> str:
    """Short fingerprint for a project_id for log correlation.

    Unlike `query_hash`, this is informational (project_id is a UUID
    already, not PII per se), but keeping logs compact matters at volume.
    We emit the first 8 chars of the UUID which is enough to identify
    a project in a single install without pasting the full UUID into
    every log line.
    """
    if project_id is None:
        return "<none>"
    s = str(project_id)
    return s[:8] if len(s) >= 8 else s
