"""SEC-11 — the diagnostic flow listing uses the canonical service-key check.

`GET /internal/v1/integrations/flows` used to carry its own `!=` comparison
of X-Service-Key: not constant-time, and blind to
FASTAPI_SERVICE_KEY_PREVIOUS, so it 401'd callers mid-rotation that every
other router accepted. It now depends on `verify_service_key`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import integrations_trigger

_PRIMARY = "p" * 48
_PREVIOUS = "q" * 48


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "FASTAPI_SERVICE_KEY", _PRIMARY, raising=False)
    monkeypatch.setattr(
        settings, "FASTAPI_SERVICE_KEY_PREVIOUS", _PREVIOUS, raising=False
    )

    async def _names() -> list[str]:
        return ["demo_flow"]

    monkeypatch.setattr(integrations_trigger, "list_flow_names", _names)

    app = FastAPI()
    app.include_router(integrations_trigger.router)
    with TestClient(app) as c:
        yield c


def test_the_listing_depends_on_the_canonical_check() -> None:
    route = next(
        r
        for r in integrations_trigger.router.routes
        if getattr(r, "path", "") == "/internal/v1/integrations/flows"
    )
    calls = {d.call for d in route.dependant.dependencies}  # type: ignore[attr-defined]
    from app.services.auth import verify_service_key

    assert verify_service_key in calls
    assert not hasattr(integrations_trigger, "_check_diagnostic_auth")


def test_primary_key_is_accepted(client: TestClient) -> None:
    r = client.get(
        "/internal/v1/integrations/flows", headers={"X-Service-Key": _PRIMARY}
    )
    assert r.status_code == 200
    assert r.json() == {"flows": ["demo_flow"]}


def test_previous_key_is_accepted_during_rotation(client: TestClient) -> None:
    r = client.get(
        "/internal/v1/integrations/flows", headers={"X-Service-Key": _PREVIOUS}
    )
    assert r.status_code == 200


def test_wrong_key_is_rejected(client: TestClient) -> None:
    r = client.get(
        "/internal/v1/integrations/flows", headers={"X-Service-Key": "x" * 48}
    )
    assert r.status_code == 401


def test_missing_key_is_rejected(client: TestClient) -> None:
    r = client.get("/internal/v1/integrations/flows")
    assert r.status_code in (401, 422)
