"""Single source of truth for Qdrant connection kwargs.

Every ``AsyncQdrantClient(...)`` in this codebase reads the same three env
vars, and several of them also need ``https=``. Azure Container Apps is why:
its internal ingress (transport=Auto) fronts each app on 443 through an
Envoy proxy and does NOT route to the container's own target port, so
Qdrant there is ``https://<app>.internal.<env>...:443`` rather than the
``http://qdrant:6333`` that local compose uses. Reading the toggle in one
place keeps a deployment from being half-migrated — which is exactly how
the embed path silently failed with ``ResponseHandlingException: timed out``
while every other Qdrant caller looked fine.
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
