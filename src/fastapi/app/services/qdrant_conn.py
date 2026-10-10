"""Single source of truth for Qdrant connection kwargs.

Every ``AsyncQdrantClient(...)`` in this codebase reads the same env vars,
and several of them also need ``https=`` (a TLS-fronted Qdrant listens on 443
rather than the ``http://qdrant:6333`` that local compose uses). Reading the
toggle in one place keeps a deployment from being half-migrated -- one caller
on plain HTTP while the rest use TLS fails as a timeout on that caller alone,
while every other Qdrant caller looks fine.
"""

from __future__ import annotations

import os

#: Request timeout for a client that does not pick its own. The Qdrant client's
#: default is httpx's 5 s, which is right for a probe and wrong for ingest: an
#: upsert of a few hundred points with payloads, or an exact count over a large
#: workspace, routinely needs longer, and a timeout there is a failed write that
#: the outbox then retries. Tunable with QDRANT_CLIENT_TIMEOUT_S.
DEFAULT_CLIENT_TIMEOUT_S = 60


def qdrant_client_kwargs(timeout: float | None = None) -> dict:
    """Return ``AsyncQdrantClient``/``QdrantClient`` connection kwargs from env.

    ``timeout`` (seconds) is part of the returned kwargs, so a caller that wants
    its own passes it HERE and not as a second ``timeout=`` beside
    ``**qdrant_client_kwargs()``, which is a duplicate-keyword TypeError. None
    means ``QDRANT_CLIENT_TIMEOUT_S`` (default 60). The query path
    (``main.py``) passes ``TIMEOUT_QDRANT_S``; liveness probes pass a few
    seconds.
    """
    if timeout is None:
        timeout = float(os.environ.get("QDRANT_CLIENT_TIMEOUT_S") or DEFAULT_CLIENT_TIMEOUT_S)
    return {
        "host": os.environ.get("QDRANT_HOST", "qdrant"),
        "port": int(os.environ.get("QDRANT_PORT", "6333")),
        "api_key": os.environ.get("QDRANT_API_KEY") or None,
        "https": os.environ.get("QDRANT_HTTPS", "").lower() in ("1", "true", "yes"),
        # The client types this as int; a fractional value would be a footgun.
        "timeout": max(1, int(timeout)),
    }
