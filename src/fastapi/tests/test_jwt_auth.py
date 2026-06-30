"""B7 FastAPI — JWT validation dependency.

Module 9 Chunk 9.4 update — extract_user_context now requires a Request
parameter (the path is read to allow unauthenticated /health, /ready,
/metrics probes). Missing-header on a non-probe path now raises 401
(was: returned empty UserContext during the graceful-rollout window).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import HTTPException

from app.services.auth import UserContext, extract_user_context

TEST_SECRET = "test-shared-secret-abcdef1234567890_padded-to-at-least-32-bytes"


def _make_request(path: str = "/v1/queries") -> MagicMock:
    """Build a minimal FastAPI Request stub with just the URL path attribute.

    extract_user_context only reads request.url.path; everything else can
    be mocked away.
    """
    req = MagicMock()
    req.url.path = path
    return req


def _mint(
    *,
    user_id: str = "user-123",
    project_id: str = "proj-abc",
    roles: list[str] | None = None,
    exp_delta: int = 60,
    iss: str = "georag-laravel",
    aud: str = "georag-fastapi",
    secret: str = TEST_SECRET,
) -> str:
    now = int(time.time())
    payload = {
        "iss": iss,
        "aud": aud,
        "sub": user_id,
        "project_id": project_id,
        "roles": roles or [],
        "iat": now,
        "exp": now + exp_delta,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def _patched_settings():
    # `settings` is imported lazily inside extract_user_context() so we
    # patch the actual attribute on the config module.
    from app.config import settings as real_settings

    original = real_settings.FASTAPI_SERVICE_KEY
    # Pydantic v2 BaseSettings objects allow attribute assignment.
    object.__setattr__(real_settings, "FASTAPI_SERVICE_KEY", TEST_SECRET)
    try:
        yield real_settings
    finally:
        object.__setattr__(real_settings, "FASTAPI_SERVICE_KEY", original)


@pytest.mark.asyncio
async def test_no_authorization_header_on_non_probe_path_raises_401():
    """Module 9 Chunk 9.4 — missing Authorization on /v1/* must return 401."""
    req = _make_request("/v1/queries/stream")
    with pytest.raises(HTTPException) as exc_info:
        await extract_user_context(request=req, authorization=None)
    assert exc_info.value.status_code == 401
    assert "Authorization header required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_no_authorization_header_on_health_returns_anonymous():
    """Health probes are unauthenticated by design (k8s)."""
    req = _make_request("/health")
    ctx = await extract_user_context(request=req, authorization=None)
    assert isinstance(ctx, UserContext)
    assert ctx.user_id is None


@pytest.mark.asyncio
async def test_no_authorization_header_on_metrics_returns_anonymous():
    """Prometheus scrapes are unauthenticated by design."""
    req = _make_request("/metrics")
    ctx = await extract_user_context(request=req, authorization=None)
    assert ctx.user_id is None


@pytest.mark.asyncio
async def test_no_authorization_header_on_ready_returns_anonymous():
    req = _make_request("/ready")
    ctx = await extract_user_context(request=req, authorization=None)
    assert ctx.user_id is None


@pytest.mark.asyncio
async def test_valid_jwt_populates_user_context(_patched_settings):
    token = _mint(user_id="user-42", project_id="proj-xyz", roles=["member"])
    req = _make_request("/v1/queries")
    ctx = await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert ctx.user_id == "user-42"
    assert ctx.project_id == "proj-xyz"
    assert ctx.roles == ("member",)


@pytest.mark.asyncio
async def test_expired_jwt_raises_401(_patched_settings):
    token = _mint(exp_delta=-5)  # issued 5s ago, already expired
    req = _make_request("/v1/queries")
    with pytest.raises(HTTPException) as exc_info:
        await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_wrong_audience_raises_401(_patched_settings):
    token = _mint(aud="some-other-service")
    req = _make_request("/v1/queries")
    with pytest.raises(HTTPException) as exc_info:
        await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401
    assert "audience" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_wrong_issuer_raises_401(_patched_settings):
    token = _mint(iss="some-other-issuer")
    req = _make_request("/v1/queries")
    with pytest.raises(HTTPException) as exc_info:
        await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401
    assert "issuer" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_tampered_signature_raises_401(_patched_settings):
    """Token signed with a DIFFERENT secret must be rejected."""
    token = _mint(secret="wrong-secret")
    req = _make_request("/v1/queries")
    with pytest.raises(HTTPException) as exc_info:
        await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_non_bearer_scheme_raises_401(_patched_settings):
    """Module 9 Chunk 9.4 — non-Bearer scheme on /v1/* must return 401.

    (Was: returned empty UserContext during graceful rollout. Now closed.)
    """
    req = _make_request("/v1/queries")
    with pytest.raises(HTTPException) as exc_info:
        await extract_user_context(request=req, authorization="Basic dXNlcjpwYXNz")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_jwt_with_primary_kid_header_decodes(_patched_settings):
    """V1.5-03 — JWT minted with the active `kid` decodes via the primary key."""
    from app.config import settings

    object.__setattr__(settings, "FASTAPI_SERVICE_KEY_KID", "primary")

    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "georag-laravel",
            "aud": "georag-fastapi",
            "sub": "user-1",
            "iat": now,
            "exp": now + 60,
        },
        TEST_SECRET,
        algorithm="HS256",
        headers={"kid": "primary"},
    )
    req = _make_request("/v1/queries")
    ctx = await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert ctx.user_id == "user-1"


@pytest.mark.asyncio
async def test_jwt_with_previous_kid_header_decodes_during_rotation(_patched_settings):
    """V1.5-03 — during rotation, tokens minted with the OLD kid still decode
    via the FASTAPI_SERVICE_KEY_PREVIOUS secret.
    """
    from app.config import settings
    previous_secret = "previous-rotation-secret-padded-to-at-least-32-bytes-long-XX"

    object.__setattr__(settings, "FASTAPI_SERVICE_KEY_KID", "primary")
    object.__setattr__(settings, "FASTAPI_SERVICE_KEY_PREVIOUS", previous_secret)
    object.__setattr__(settings, "FASTAPI_SERVICE_KEY_PREVIOUS_KID", "rot-2026-q2")

    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "georag-laravel",
            "aud": "georag-fastapi",
            "sub": "user-rot",
            "iat": now,
            "exp": now + 60,
        },
        previous_secret,
        algorithm="HS256",
        headers={"kid": "rot-2026-q2"},
    )
    req = _make_request("/v1/queries")
    ctx = await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert ctx.user_id == "user-rot"

    # Cleanup so other tests don't see the rotation overlap.
    object.__setattr__(settings, "FASTAPI_SERVICE_KEY_PREVIOUS", "")
    object.__setattr__(settings, "FASTAPI_SERVICE_KEY_PREVIOUS_KID", "")


@pytest.mark.asyncio
async def test_jwt_with_unknown_kid_raises_401(_patched_settings):
    """V1.5-03 — kid that doesn't match either primary or previous → 401."""
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "georag-laravel",
            "aud": "georag-fastapi",
            "sub": "user-bogus",
            "iat": now,
            "exp": now + 60,
        },
        TEST_SECRET,
        algorithm="HS256",
        headers={"kid": "this-kid-does-not-exist"},
    )
    req = _make_request("/v1/queries")
    with pytest.raises(HTTPException) as exc_info:
        await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401
    assert "Unknown JWT kid" in exc_info.value.detail


@pytest.mark.asyncio
async def test_jwt_without_kid_falls_back_to_primary(_patched_settings):
    """V1.5-03 — pre-rotation tokens (no kid header) decode via primary key."""
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "georag-laravel",
            "aud": "georag-fastapi",
            "sub": "user-legacy",
            "iat": now,
            "exp": now + 60,
        },
        TEST_SECRET,
        algorithm="HS256",
        # No headers={} — no kid emitted.
    )
    req = _make_request("/v1/queries")
    ctx = await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert ctx.user_id == "user-legacy"


@pytest.mark.asyncio
async def test_jwt_within_2s_leeway_still_accepted(_patched_settings):
    """Module 9 Chunk 9.8 (A4-01) — 2-second clock skew tolerance.

    A token that expired 1 second ago should still decode because PyJWT's
    leeway=2 absorbs minor NTP drift. A token that expired 5 seconds ago
    is past the leeway window and must 401.
    """
    # Within leeway: expired 1s ago.
    token_within = _mint(exp_delta=-1)
    req = _make_request("/v1/queries")
    ctx = await extract_user_context(request=req, authorization=f"Bearer {token_within}")
    assert ctx.user_id == "user-123"

    # Outside leeway: expired 5s ago.
    token_past = _mint(exp_delta=-5)
    with pytest.raises(HTTPException) as exc_info:
        await extract_user_context(request=req, authorization=f"Bearer {token_past}")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_jwt_workspace_id_claim_populates_user_context(_patched_settings):
    """Module 9 Chunk 9.4 — workspace_id claim flows through to UserContext."""
    now = int(time.time())
    payload = {
        "iss": "georag-laravel",
        "aud": "georag-fastapi",
        "sub": "user-42",
        "project_id": "proj-xyz",
        "workspace_id": "11111111-1111-1111-1111-111111111111",
        "iat": now,
        "exp": now + 60,
    }
    token = jwt.encode(payload, TEST_SECRET, algorithm="HS256")
    req = _make_request("/v1/queries")
    ctx = await extract_user_context(request=req, authorization=f"Bearer {token}")
    assert ctx.workspace_id == "11111111-1111-1111-1111-111111111111"
