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

import logging
import os
import threading
import time

import asyncpg
import jwt
from fastapi import HTTPException, status

from app.config import settings
from app.db.dsn import build_dsn

logger = logging.getLogger(__name__)

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
# Thread-safe cache keyed on flow_name -> [(kid, secret), ...]. TTL matches
# the flow_registry cache so mints + verifies see consistent state.
#
# There are two entry points because there are two kinds of caller, and
# 2026-08-22 separated them:
#
#   _aget_per_flow_keys   awaited from the FastAPI route. The DB fetch is
#                         asyncpg and always was; only the wrapper was sync.
#   _get_per_flow_keys    for genuinely synchronous callers (the rotation
#                         CLI, the smoke scripts). Keeps the thread bridge,
#                         which is correct OUTSIDE a loop.
#
# Before the split, the sync wrapper detected a running loop and spawned a
# single-worker ThreadPoolExecutor per call, blocking the event loop on
# `future.result(timeout=10)` -- and the timeout was not a bound, because
# the executor is a context manager and `__exit__` waits for the worker
# regardless. Every cache miss (per flow, per worker, per 60s, and always
# after a restart) froze every concurrent SSE stream on that worker.

_PER_FLOW_KEY_TTL_SECONDS = 60
_per_flow_lock = threading.Lock()
# Phase 6 Step 3 (R-P5-2) — cache the full set of currently-valid kids
# for a flow, not just the active one. The first element (index 0) is
# the most recently activated kid (the mint target); every element is
# a valid verify target during a rotation overlap window.
_per_flow_cache: dict[str, tuple[list[tuple[str, str]], float]] = {}
# value: (list_of_(kid, secret), fetched_at_monotonic)


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn_sync = build_dsn


class FlowKeyLookupError(RuntimeError):
    """The per-flow key lookup could not be completed.

    Distinct from "this flow has no per-flow keys", which is an empty list
    and a normal state. Both used to be `[]`: `except Exception: return []`
    turned an unreachable database, a missing AUDIT_ENCRYPTION_KEY and a
    permissions error into the same value, after which the caller fell
    back to the global env-var key with nothing logged. A silent auth
    downgrade that reads as normal operation.
    """


async def _fetch_per_flow_keys(flow_name: str) -> list[tuple[str, str]]:
    """The actual DB call. asyncpg, and always was.

    Returns (kid, plaintext) for every kid whose valid window includes
    now(); an empty list when the flow has no per-flow keys configured.
    The first element is the most recently activated kid (the mint
    target); every element is a valid verify target during a rotation
    overlap (Phase 6 Step 3 / R-P5-2).

    Raises FlowKeyLookupError when the lookup itself failed.
    """
    enc_key = os.environ.get("AUDIT_ENCRYPTION_KEY", "")
    if not enc_key:
        # Not an error: a deployment with no per-flow keys provisioned
        # never sets this. Logged at debug so the env-var fallback that
        # follows is traceable.
        logger.debug(
            "flow_jwt: AUDIT_ENCRYPTION_KEY unset — no per-flow keys for %s",
            flow_name,
        )
        return []

    try:
        conn = await asyncpg.connect(_dsn_sync())
    except Exception as exc:
        raise FlowKeyLookupError(
            f"could not connect to look up per-flow keys for {flow_name}"
        ) from exc

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
    except Exception as exc:
        raise FlowKeyLookupError(
            f"per-flow key lookup failed for {flow_name}"
        ) from exc
    finally:
        await conn.close()


def _load_per_flow_keys_sync(flow_name: str) -> list[tuple[str, str]]:
    """Blocking wrapper, for callers that are genuinely synchronous.

    The rotation CLI and the smoke scripts. NOT the FastAPI route — see
    `_aget_per_flow_keys`.

    Refuses to run inside an event loop rather than bridging into one.
    The bridge is what made this a blocker: it stopped the loop for an
    unbounded connect / query / close. If this raises, the caller is an
    async context that should be awaiting the async path instead.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "_load_per_flow_keys_sync called from a running event loop; "
            "await _aget_per_flow_keys() instead. Bridging to a thread "
            "here blocks the loop for the whole DB round trip."
        )

    return asyncio.run(_fetch_per_flow_keys(flow_name))


def _cached_keys(flow_name: str) -> list[tuple[str, str]] | None:
    """Live cache entry for this flow, or None when it must be fetched."""
    now = time.monotonic()
    with _per_flow_lock:
        entry = _per_flow_cache.get(flow_name)
    if entry is None:
        return None
    keys, fetched_at = entry
    if (now - fetched_at) >= _PER_FLOW_KEY_TTL_SECONDS:
        return None
    return keys


def _store_keys(flow_name: str, keys: list[tuple[str, str]]) -> None:
    with _per_flow_lock:
        _per_flow_cache[flow_name] = (keys, time.monotonic())


async def _aget_per_flow_keys(flow_name: str) -> list[tuple[str, str]]:
    """Cached per-flow key lookup, for async callers.

    This is the path the FastAPI route takes. Returns every (kid, secret)
    currently inside its valid window; the first element is the mint
    target.

    A failed LOOKUP propagates as FlowKeyLookupError rather than becoming
    an empty list, so an unreachable database surfaces as a 503 instead
    of silently downgrading auth to the global env-var key. A flow with
    no per-flow keys still returns [] — that is a normal state, and the
    env-var fallback below it is the intended behaviour.
    """
    cached = _cached_keys(flow_name)
    if cached is not None:
        return cached

    keys = await _fetch_per_flow_keys(flow_name)
    _store_keys(flow_name, keys)
    return keys


def _get_per_flow_keys(flow_name: str) -> list[tuple[str, str]]:
    """Cached per-flow key lookup, for synchronous callers.

    Same contract as `_aget_per_flow_keys`; see `_load_per_flow_keys_sync`
    for why this one refuses to run inside an event loop.
    """
    cached = _cached_keys(flow_name)
    if cached is not None:
        return cached

    keys = _load_per_flow_keys_sync(flow_name)
    _store_keys(flow_name, keys)
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


async def averify_flow_jwt_token(token: str, expected_flow_name: str) -> dict:
    """Async verification — the path FastAPI routes take.

    Identical to `verify_flow_jwt_token` except that the key lookup is
    awaited instead of bridged into a thread. The sync version remains
    for the rotation CLI and the smoke scripts.
    """
    inbound_kid = _peek_kid(token)
    try:
        per_flow_keys = await _aget_per_flow_keys(expected_flow_name)
    except FlowKeyLookupError as exc:
        # Deliberately NOT an empty list. Falling through to the env-var
        # key here would downgrade auth because the database was
        # unreachable, and would look identical to a flow that simply has
        # no per-flow key.
        logger.error(
            "flow_jwt: per-flow key lookup failed for %s — refusing to fall "
            "back to the global key", expected_flow_name, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="flow key lookup unavailable",
        ) from exc

    return _verify_with_keys(token, expected_flow_name, inbound_kid, per_flow_keys)


def _peek_kid(token: str) -> str | None:
    """Read the `kid` header without verifying the signature."""
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"flow JWT malformed: {e}",
        ) from e
    return unverified_header.get("kid")


def verify_flow_jwt_token(token: str, expected_flow_name: str) -> dict:
    """Pure verification — no FastAPI types. Phase 5 Step 2: if the
    JWT carries a ``kid`` header, look up the matching per-flow key;
    otherwise verify against the env-var fallback. The decoded scope
    must still match ``flow:<expected_flow_name>``.

    Synchronous. Async callers must use `averify_flow_jwt_token`.
    """
    inbound_kid = _peek_kid(token)
    per_flow_keys = _get_per_flow_keys(expected_flow_name)

    return _verify_with_keys(token, expected_flow_name, inbound_kid, per_flow_keys)


def _verify_with_keys(
    token: str,
    expected_flow_name: str,
    inbound_kid: str | None,
    per_flow_keys: list[tuple[str, str]],
) -> dict:
    """The verification itself, shared by both entry points."""

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
    "averify_flow_jwt_token",
    "verify_flow_jwt_token",
    "ISSUER",
    "AUDIENCE",
    "ALGORITHM",
    "DEFAULT_TTL_SECONDS",
]
