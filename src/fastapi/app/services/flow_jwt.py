"""Phase 3 Step 3 / Phase 5 Step 2 — per-flow JWT mint + verify.

The Kestra → FastAPI integrations bridge uses per-flow JWTs. Each
Kestra flow holds its own JWT in Kestra's secret store; rotation is
per-flow rather than global, so a leak compromises one flow rather
than every integration.

Auth shape:
  - Header: ``Authorization: Bearer <jwt>``
  - Algorithm: HS256
  - Issuer: ``georag-kestra``
  - Audience: ``georag-fastapi-flows``
  - Required claims: ``exp``, ``iat``, ``scope``
  - Scope: exactly ``flow:<flow_name>``
  - Optional ``kid`` claim: when present, selects a per-flow signing
    secret from ``workflow.flow_registry`` (Phase 5 Step 2). When
    absent, the shared ``settings.KESTRA_FLOW_JWT_SECRET`` env var is
    the signing/verify key (Phase 3 behavior).

Per-flow key resolution (Phase 5):
  - On mint: if the flow has ``jwt_secret_kid`` + ``jwt_secret_ciphertext``
    set in the registry, mint uses that key + sets the ``kid`` claim;
    otherwise the env-var fallback is used and ``kid`` is omitted.
  - On verify: if ``kid`` claim is present, look up the per-flow secret
    by ``flow_name`` and require its kid matches; otherwise fall back
    to the env-var.

This means rotating one flow's JWT key only invalidates that flow's
tokens — the others keep verifying against their own keys (or the env
fallback).
"""

from __future__ import annotations

import os
import threading
import time

import asyncpg
import jwt
from fastapi import HTTPException, status

from app.config import settings

ISSUER = "georag-kestra"
AUDIENCE = "georag-fastapi-flows"
ALGORITHM = "HS256"

# 24h default — Kestra holds these in its secret store; rotation is
# operator-driven via scripts/phase3_jwt_rotate.sh, not per-request.
DEFAULT_TTL_SECONDS = 24 * 60 * 60

# Small clock-skew tolerance, matches services/auth.py.
LEEWAY_SECONDS = 2


# ---------------------------------------------------------------------------
# Per-flow key cache (Phase 5 Step 2)
# ---------------------------------------------------------------------------
# Synchronous + thread-safe cache keyed on flow_name → (kid, secret).
# TTL matches the flow_registry cache so mints + verifies see consistent
# state. The DB lookup is sync (psycopg2) because mint_flow_jwt is called
# from sync contexts (rotation CLI, smoke). Verify also uses this path.

_PER_FLOW_KEY_TTL_SECONDS = 60
_per_flow_lock = threading.Lock()
# Phase 6 Step 3 (R-P5-2) — cache the full set of currently-valid kids
# for a flow, not just the active one. The first element (index 0) is
# the most recently activated kid (the mint target); every element is
# a valid verify target during a rotation overlap window.
_per_flow_cache: dict[str, tuple[list[tuple[str, str]], float]] = {}
# value: (list_of_(kid, secret), fetched_at_monotonic)


def _dsn_sync() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _load_per_flow_keys_sync(flow_name: str) -> list[tuple[str, str]]:
    """Synchronous DB fetch — calls workflow.get_flow_jwt_keys(flow).
    Returns a list of (kid, plaintext) for every kid whose valid window
    includes now(); empty list if no per-flow keys are configured.

    The first element is the most recently activated kid (mint target);
    every element is a valid verify target during a rotation overlap
    (Phase 6 Step 3 / R-P5-2)."""
    import asyncio

    enc_key = os.environ.get("AUDIT_ENCRYPTION_KEY", "")
    if not enc_key:
        return []

    async def _fetch() -> list[tuple[str, str]]:
        conn = await asyncpg.connect(_dsn_sync())
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.audit_encryption_key', $1, true)",
                    enc_key,
                )
                rows = await conn.fetch(
                    "SELECT kid, plain FROM workflow.get_flow_jwt_keys($1)",
                    flow_name,
                )
            return [(r["kid"], r["plain"]) for r in rows]
        finally:
            await conn.close()

    try:
        try:
            asyncio.get_running_loop()
            inside_loop = True
        except RuntimeError:
            inside_loop = False

        if inside_loop:
            # Called from inside an async context (FastAPI route handler).
            # asyncio.run() can't nest; run _fetch in a worker thread
            # with its own event loop instead.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(lambda: asyncio.run(_fetch()))
                return future.result(timeout=10)

        return asyncio.run(_fetch())
    except Exception:
        return []


def _get_per_flow_keys(flow_name: str) -> list[tuple[str, str]]:
    """Cached per-flow key lookup. Returns the full list of (kid,
    secret) currently in their valid window; first element is the
    mint target."""
    now = time.monotonic()
    with _per_flow_lock:
        entry = _per_flow_cache.get(flow_name)
        if entry is not None:
            keys, fetched_at = entry
            if (now - fetched_at) < _PER_FLOW_KEY_TTL_SECONDS:
                return keys
    keys = _load_per_flow_keys_sync(flow_name)
    with _per_flow_lock:
        _per_flow_cache[flow_name] = (keys, now)
    return keys


def _get_per_flow_key(flow_name: str) -> tuple[str | None, str | None]:
    """Back-compat shim — returns the active mint key (kid, secret) or
    (None, None). Kept so external callers + the Phase 5 Step 2 verifier
    still work."""
    keys = _get_per_flow_keys(flow_name)
    if not keys:
        return None, None
    return keys[0]


def _resolve_signing_key(flow_name: str) -> tuple[str, str | None]:
    """Return (secret, kid). kid is None when falling back to env-var."""
    kid, secret = _get_per_flow_key(flow_name)
    if secret:
        return secret, kid
    env_secret = getattr(settings, "KESTRA_FLOW_JWT_SECRET", "") or ""
    if not env_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="no per-flow key and KESTRA_FLOW_JWT_SECRET not configured",
        )
    return env_secret, None


def mint_flow_jwt(flow_name: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint a JWT for a single flow_name. If the flow has a per-flow
    key in workflow.flow_registry, that key signs the token + the
    ``kid`` claim is set. Otherwise the env-var fallback signs and no
    ``kid`` claim is emitted."""
    secret, kid = _resolve_signing_key(flow_name)
    now = int(time.time())
    headers = {"kid": kid} if kid else None
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "kestra",
            "scope": f"flow:{flow_name}",
            "iat": now,
            "exp": now + ttl_seconds,
        },
        secret,
        algorithm=ALGORITHM,
        headers=headers,
    )


def verify_flow_jwt_token(token: str, expected_flow_name: str) -> dict:
    """Pure verification — no FastAPI types. Phase 5 Step 2: if the
    JWT carries a ``kid`` header, look up the matching per-flow key;
    otherwise verify against the env-var fallback. The decoded scope
    must still match ``flow:<expected_flow_name>``."""
    # Peek the kid without verifying signature.
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"flow JWT malformed: {e}",
        ) from e

    inbound_kid = unverified_header.get("kid")
    per_flow_keys = _get_per_flow_keys(expected_flow_name)

    if inbound_kid is not None:
        # Phase 6 Step 3 (R-P5-2) — token was minted with a per-flow
        # key. Match the inbound kid against ANY currently-valid kid;
        # during a rotation overlap, both the old + new kid verify.
        verify_key = next(
            (secret for kid, secret in per_flow_keys if kid == inbound_kid),
            None,
        )
        if verify_key is None:
            registry_kids = [kid for kid, _ in per_flow_keys]
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"flow JWT kid mismatch: token={inbound_kid!r} "
                       f"valid_kids={registry_kids!r}",
            )
    else:
        # No kid → env-var fallback. The flow may or may not have a
        # per-flow key; the env-var still verifies tokens minted
        # before the per-flow key was provisioned.
        env_secret = getattr(settings, "KESTRA_FLOW_JWT_SECRET", "") or ""
        if not env_secret:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="KESTRA_FLOW_JWT_SECRET not configured",
            )
        verify_key = env_secret

    try:
        claims = jwt.decode(
            token,
            verify_key,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "scope"]},
            leeway=LEEWAY_SECONDS,
        )
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="flow JWT expired",
        ) from e
    except jwt.InvalidAudienceError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="flow JWT audience mismatch",
        ) from e
    except jwt.InvalidIssuerError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="flow JWT issuer mismatch",
        ) from e
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"flow JWT invalid: {e}",
        ) from e

    scope = claims.get("scope", "")
    expected = f"flow:{expected_flow_name}"
    if scope != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"flow JWT scope mismatch: got {scope!r}, expected {expected!r}",
        )
    return claims


def invalidate_per_flow_key_cache(flow_name: str | None = None) -> None:
    """Operator-facing — drop one flow's cache entry (or all). Called by
    the rotation helper so a just-rotated key takes effect immediately
    without waiting for the TTL."""
    with _per_flow_lock:
        if flow_name is None:
            _per_flow_cache.clear()
        else:
            _per_flow_cache.pop(flow_name, None)


__all__ = [
    "mint_flow_jwt",
    "verify_flow_jwt_token",
    "ISSUER",
    "AUDIENCE",
    "ALGORITHM",
    "DEFAULT_TTL_SECONDS",
]
