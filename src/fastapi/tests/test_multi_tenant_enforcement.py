"""Tests for P0 #4 — multi-tenant project boundary enforcement.

When ``MULTI_TENANT_ENFORCEMENT_ENABLED=True``, the ``/internal/queries``
route must:

  * Refuse requests whose JWT has no ``project_id`` claim (403).
  * Refuse requests where the JWT's ``project_id`` doesn't equal the request
    body's ``project_id`` (403).
  * Honour requests where the JWT and body project_ids match.

When the flag is False (graceful rollout, default), the route must accept
all of the above and only log a warning on mismatch — a single-tenant
deployment needs to keep working while Laravel rolls out the JWT minter.

These tests exercise the route logic directly (FastAPI ``TestClient``-style)
rather than spinning up pools. They rely on the fact that
``post_query`` performs the enforcement check BEFORE touching app.state
pools, so the body/app.state mismatch never matters.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.auth import UserContext


@pytest.fixture
def _flag(monkeypatch):
    """Toggle the MULTI_TENANT_ENFORCEMENT_ENABLED setting per test."""
    from app.config import settings

    original = settings.MULTI_TENANT_ENFORCEMENT_ENABLED

    def _set(value: bool) -> None:
        object.__setattr__(settings, "MULTI_TENANT_ENFORCEMENT_ENABLED", value)

    yield _set
    object.__setattr__(settings, "MULTI_TENANT_ENFORCEMENT_ENABLED", original)


def _fake_request():
    """Minimal Request stand-in — the enforcement check never touches it."""

    class _App:
        state = type("S", (), {})()

    class _Req:
        app = _App()

    return _Req()


def _fake_body(project_id: str = "proj-abc"):
    """Minimal QueryRequest stand-in — only project_id is consulted."""

    class _Body:
        query = "test"

        def __init__(self, pid: str):
            self.project_id = pid

    return _Body(project_id)


@pytest.mark.asyncio
async def test_enforcement_off_allows_missing_jwt(_flag):
    """Graceful rollout default — missing JWT project_id must NOT 403."""
    from app.routers.queries import post_query

    _flag(False)
    user = UserContext(user_id=None, project_id=None, roles=())
    # No exception → enforcement is off. The call does return a
    # StreamingResponse we don't iterate; that's fine — we're only
    # checking the precondition guard.
    resp = await post_query(_fake_body(), _fake_request(), user=user)
    assert resp is not None


@pytest.mark.asyncio
async def test_enforcement_on_missing_jwt_rejects(_flag):
    from app.routers.queries import post_query

    _flag(True)
    user = UserContext(user_id=None, project_id=None, roles=())
    with pytest.raises(HTTPException) as exc_info:
        await post_query(_fake_body("proj-abc"), _fake_request(), user=user)
    assert exc_info.value.status_code == 403
    assert "project_id claim" in exc_info.value.detail


@pytest.mark.asyncio
async def test_enforcement_on_project_mismatch_rejects(_flag):
    from app.routers.queries import post_query

    _flag(True)
    user = UserContext(user_id="u1", project_id="proj-DIFFERENT", roles=())
    with pytest.raises(HTTPException) as exc_info:
        await post_query(_fake_body("proj-abc"), _fake_request(), user=user)
    assert exc_info.value.status_code == 403
    assert "does not match" in exc_info.value.detail


@pytest.mark.asyncio
async def test_enforcement_on_matching_project_passes(_flag):
    """Sanity: when JWT and body agree, the check lets the request through."""
    from app.routers.queries import post_query

    _flag(True)
    user = UserContext(user_id="u1", project_id="proj-abc", roles=())
    # If enforcement raised, this would throw; a real StreamingResponse is
    # returned (we don't iterate it).
    resp = await post_query(_fake_body("proj-abc"), _fake_request(), user=user)
    assert resp is not None
