"""Service-to-service authentication dependency.

Laravel sends every internal request with two auth credentials (graceful
rollout of B7):
  - `X-Service-Key` — the legacy shared-secret header. Still required.
  - `Authorization: Bearer <JWT>` — short-TTL (60 s) JWT minted by Laravel
    (app/Services/FastApiJwtMinter.php) with the same shared secret as the
    HS256 HMAC key. Carries `sub` (user_id), `project_id`, and `roles`.

Usage
-----
Declare as a dependency on any router or individual route:

    from app.services.auth import verify_service_key, extract_user_context

    router = APIRouter(dependencies=[Depends(verify_service_key)])

    @router.post("/queries")
    async def post_query(user: UserContext = Depends(extract_user_context), ...):
        ...

This is an async function so FastAPI handles it without spawning a thread.
The comparison uses hmac.compare_digest to prevent timing attacks.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request, status

logger = logging.getLogger(__name__)

# Module 9 Chunk 9.4 (A1-02) — paths where missing Authorization is OK.
# Health/readiness probes are unauthenticated by design (k8s probes,
# Prometheus scrapes). Every other path under /v1, /internal, etc. now
# requires a valid Bearer JWT.
_AUTH_OPTIONAL_PATH_PREFIXES: tuple[str, ...] = ("/health", "/ready", "/metrics")


async def verify_service_key(x_service_key: str = Header(...)) -> None:
    """Validate the X-Service-Key header against the configured shared secret.

    Raises HTTP 401 if the key is missing (FastAPI raises 422 automatically
    when the Header annotation is non-optional and the header is absent, which
    is caught by the global exception handler and surfaced to Laravel as a
    structured error). An incorrect key raises HTTP 401 explicitly.

    Uses hmac.compare_digest for constant-time comparison — prevents an
    attacker from inferring the correct key length via response-time analysis.
    """
    from app.config import settings

    expected = settings.FASTAPI_SERVICE_KEY
    if not hmac.compare_digest(x_service_key.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service key",
        )


# ---------------------------------------------------------------------------
# B7 — JWT-scoped user context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserContext:
    """Resolved request-scoped identity, populated from the Laravel-minted JWT.

    user_id / project_id are None when the JWT is absent (graceful rollout
    — X-Service-Key alone remains valid today). Tools that want user-level
    RBAC should check for None and degrade gracefully.
    """

    user_id: str | None = None
    project_id: str | None = None
    # Module 9 Chunk 9.4 — workspace_id may arrive directly on the JWT
    # (preferred) or be derived later via project_id → DB lookup in
    # services.workspace_resolution.
    workspace_id: str | None = None
    roles: tuple[str, ...] = ()


async def extract_user_context(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> UserContext:
    """Decode + validate the Laravel-minted JWT on `Authorization: Bearer …`.

    Contract (mirror Laravel's FastApiJwtMinter):
      - Algorithm: HS256
      - Signing key: settings.FASTAPI_SERVICE_KEY
      - iss: "georag-laravel"
      - aud: "georag-fastapi"
      - TTL: 60 seconds (enforced by `exp` via PyJWT's default checks)

    Module 9 Chunk 9.4 (A1-02) — missing Authorization header now raises 401
    on every non-probe path. The graceful-rollout window where X-Service-Key
    alone could pass is closed. Health / ready / metrics probes are still
    accepted unauthenticated.

    Invalid / expired / wrong-audience / wrong-issuer → raises 401.
    """
    path = request.url.path
    is_probe = any(path.startswith(p) for p in _AUTH_OPTIONAL_PATH_PREFIXES)

    if not authorization:
        if is_probe:
            return UserContext()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        if is_probe:
            return UserContext()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization must use Bearer scheme",
        )

    try:
        import jwt  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "extract_user_context: PyJWT not installed — skipping JWT validation"
        )
        return UserContext()

    from app.config import settings  # noqa: PLC0415

    # V1.5-03 — pick the verifying key from the kid header.
    # Laravel mints with `kid: <FASTAPI_SERVICE_KEY_KID>`; we map kid → secret
    # and verify with the matching key. Missing kid (legacy minter) falls back
    # to the primary key — preserves backward compat through the rotation
    # window. Unknown kid → 401.
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid JWT header: {exc}",
        )

    kid_lookup: dict[str, str] = {
        settings.FASTAPI_SERVICE_KEY_KID: settings.FASTAPI_SERVICE_KEY,
    }
    if settings.FASTAPI_SERVICE_KEY_PREVIOUS and settings.FASTAPI_SERVICE_KEY_PREVIOUS_KID:
        kid_lookup[settings.FASTAPI_SERVICE_KEY_PREVIOUS_KID] = (
            settings.FASTAPI_SERVICE_KEY_PREVIOUS
        )

    incoming_kid = unverified_header.get("kid")
    if incoming_kid is None:
        # Pre-V1.5-03 minter — fall back to the primary key.
        verify_key = settings.FASTAPI_SERVICE_KEY
    elif incoming_kid in kid_lookup:
        verify_key = kid_lookup[incoming_kid]
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unknown JWT kid: {incoming_kid!r}",
        )

    try:
        claims = jwt.decode(
            token,
            verify_key,
            algorithms=["HS256"],
            issuer="georag-laravel",
            audience="georag-fastapi",
            options={"require": ["exp", "iat", "sub"]},
            # Module 9 Chunk 9.8 (A4-01) — 2-second clock skew tolerance.
            # The 60-second JWT TTL means a 60s+ skew rejects valid tokens
            # silently; 2s is small enough to not weaken expiry materially
            # while accommodating typical NTP drift between Laravel and
            # FastAPI containers.
            leeway=2,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT expired",
        )
    except jwt.InvalidAudienceError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT audience mismatch",
        )
    except jwt.InvalidIssuerError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT issuer mismatch",
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid JWT: {exc}",
        )

    return UserContext(
        user_id=str(claims.get("sub")) if claims.get("sub") is not None else None,
        project_id=str(claims.get("project_id")) if claims.get("project_id") else None,
        workspace_id=str(claims.get("workspace_id")) if claims.get("workspace_id") else None,
        roles=tuple(claims.get("roles") or ()),
    )
