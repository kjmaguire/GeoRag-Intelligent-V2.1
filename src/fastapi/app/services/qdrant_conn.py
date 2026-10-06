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


def qdrant_client_kwargs() -> dict:
    """Return ``AsyncQdrantClient``/``QdrantClient`` connection kwargs from env."""
    return {
        "host": os.environ.get("QDRANT_HOST", "qdrant"),
        "port": int(os.environ.get("QDRANT_PORT", "6333")),
        "api_key": os.environ.get("QDRANT_API_KEY") or None,
        "https": os.environ.get("QDRANT_HTTPS", "").lower() in ("1", "true", "yes"),
    }
