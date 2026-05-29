"""Rate-limiter for the Laravel↔FastAPI internal API.

Why
---
A misbehaving Laravel job (or a compromised JWT in a multi-tenant world)
could fan out unbounded LLM calls before Laravel notices and pull the
plug. ``slowapi`` provides a lightweight in-process rate limiter; we key
on ``(workspace_id, user_id)`` extracted from the inbound JWT so the
quota is per-actor, not per-IP (FastAPI sees only Laravel's IP — every
caller would share a single bucket otherwise).

How it slots in
---------------
The limiter is configured at module import time. ``main.py``:

  1. Stores it on ``app.state.limiter`` (slowapi reads from there).
  2. Registers ``RateLimitExceeded`` as an exception handler so the
     response is a clean 429 with ``Retry-After``.
  3. Endpoints use ``@limiter.limit(settings.RATE_LIMIT_QUERIES)``.

When ``settings.RATE_LIMIT_ENABLED=False`` (default in dev), the
limiter is constructed with ``enabled=False`` — every ``@limit`` call
becomes a no-op so dev iteration is unaffected.

Storage backend
---------------
Default is in-process memory (per FastAPI worker). With 4 uvicorn
workers and a 20/min limit, an attacker can effectively burst at 80/min
because each worker tracks its own bucket. For tighter accuracy,
configure ``settings.RATE_LIMIT_STORAGE_URI`` to a Redis URL
(``redis://:<pw>@redis:6380/4``) and the limits library shares state
across workers. Recommended for staging/prod when 3-instance Redis
topology lands; in dev the in-process default is fine.

Key function — fast unverified JWT decode
-----------------------------------------
slowapi's key function runs in starlette middleware, BEFORE FastAPI
dependency resolution. That means ``extract_user_context`` (which would
populate ``request.state``) hasn't run yet. We therefore parse the
JWT payload here — UNVERIFIED, just to extract claims for keying. The
actual auth gate is still ``verify_service_key`` + ``extract_user_context``
on each endpoint; a forged JWT can spoof the rate-limit key, but it
cannot bypass auth.

Falls back to remote address when no JWT, malformed JWT, or missing
``workspace_id``/``sub`` claims — ensures unauthenticated paths
(misconfigured caller) are still bounded.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Final

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

logger = logging.getLogger(__name__)


def _fast_unverified_jwt_claims(authorization_header: str) -> dict[str, str] | None:
    """Decode a Bearer JWT payload WITHOUT signature verification.

    Used only for rate-limit key extraction. The verified auth gate runs
    later via ``app.services.auth.verify_service_key``.

    Returns the decoded payload dict, or ``None`` if the header is absent,
    malformed, or its base64 segment fails to decode.
    """
    if not authorization_header.startswith("Bearer "):
        return None
    token = authorization_header[len("Bearer "):]
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    # Add padding so urlsafe_b64decode accepts the segment regardless of
    # how the JWT minter chose to strip trailing '='.
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        result = json.loads(decoded)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return result if isinstance(result, dict) else None


def workspace_user_key(request: Request) -> str:
    """Build a rate-limit key from the JWT's workspace_id + sub claims.

    Falls back to ``get_remote_address`` when:
      - No Authorization header.
      - Header is not a Bearer token.
      - JWT payload cannot be base64-decoded or JSON-parsed.
      - Required claims (``workspace_id``, ``sub``) are absent.

    The fallback ensures every request is rate-limited under SOME key so a
    misconfigured caller cannot bypass the limiter by simply omitting the
    JWT — the verified auth gate will reject the request anyway, but the
    rate limiter shouldn't be a free-pass detour.
    """
    auth = request.headers.get("authorization", "")
    claims = _fast_unverified_jwt_claims(auth)
    if claims:
        ws = claims.get("workspace_id")
        sub = claims.get("sub")
        if ws and sub:
            return f"ws:{ws}:user:{sub}"
    return get_remote_address(request)


# ---------------------------------------------------------------------------
# Limiter instance
# ---------------------------------------------------------------------------
# Shared across all routers. main.py exposes it via app.state so slowapi's
# decorator path can find it.
#
# storage_uri: in-memory by default. Set settings.RATE_LIMIT_STORAGE_URI to
#   a Redis URL for shared-state across workers (recommended in prod).
#
# enabled: bound to settings.RATE_LIMIT_ENABLED. When False, every
#   @limiter.limit(...) decorator is a no-op — perfect for dev iteration
#   without removing the decorators.
# ---------------------------------------------------------------------------

_storage_uri = getattr(settings, "RATE_LIMIT_STORAGE_URI", None) or "memory://"

limiter: Final[Limiter] = Limiter(
    key_func=workspace_user_key,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    storage_uri=_storage_uri,
    enabled=settings.RATE_LIMIT_ENABLED,
    headers_enabled=True,  # emit X-RateLimit-* response headers
)

logger.info(
    "Rate limiter constructed: enabled=%s, storage=%s, default=%s, queries=%s",
    settings.RATE_LIMIT_ENABLED,
    "redis" if _storage_uri.startswith("redis") else "memory",
    settings.RATE_LIMIT_DEFAULT,
    settings.RATE_LIMIT_QUERIES,
)
