"""§04p PDF router — workspace_id enforcement regression test.

Background
----------
Every silver.pdf_* table (pdf_text_blocks, pdf_table_cells,
pdf_layout_regions, pdf_ocr_results, pdf_vl_summaries, pdf_coordinates)
has workspace_id uuid NOT NULL. The router endpoints must reject any
request that arrives without a workspace_id on the JWT — otherwise the
service layer would crash on the asyncpg insert with
NotNullViolationError and surface a generic 500.

These tests pin the contract: a UserContext without a workspace_id must
be rejected at the router layer with 401 "workspace_required", before
any service call is dispatched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.pdf import router as pdf_router
from app.services.auth import UserContext, extract_user_context, verify_service_key


_VALID_PDF_ID = "1f431fb8a386ace41052ae90821522909eb92c7495608de3f6420d3d54af33a4"


def _make_app(user: UserContext) -> FastAPI:
    """Mount the pdf router and override auth to return the given UserContext.

    All downstream services are intentionally left absent on app.state —
    the workspace_id guard must trip BEFORE the router tries to use them,
    so the test never reaches the service-not-ready 503 path.
    """
    app = FastAPI()
    app.include_router(pdf_router)
    app.dependency_overrides[verify_service_key] = lambda: None
    app.dependency_overrides[extract_user_context] = lambda: user
    return app


def _make_app_with_services(user: UserContext) -> FastAPI:
    """Like _make_app but wires async-mockable bronze + service state.

    Used by the happy-path test that proves a valid workspace_id passes
    the router guard and reaches the service layer.
    """
    app = _make_app(user)

    bronze = MagicMock()
    bronze.get = AsyncMock(return_value=b"%PDF-1.4 stub")
    app.state.bronze_store = bronze

    extract_service = MagicMock()
    extract_service.extract_text = AsyncMock(return_value=([], False))
    app.state.pdf_extract_service = extract_service
    return app


@pytest.mark.parametrize(
    ("method", "path", "params"),
    [
        ("GET", "/pdf/extract_text", {"pdf_id": _VALID_PDF_ID, "page": 1}),
        ("GET", "/pdf/find_tables", {"pdf_id": _VALID_PDF_ID, "page": 1}),
        ("GET", "/pdf/find_legends", {"pdf_id": _VALID_PDF_ID, "page": 1}),
        ("GET", "/pdf/find_coordinates", {"pdf_id": _VALID_PDF_ID, "page": 1}),
        (
            "GET",
            "/pdf/summarize_section",
            {"pdf_id": _VALID_PDF_ID, "section_kind": "page", "page": 1},
        ),
    ],
)
def test_workspace_id_missing_returns_401_on_get_endpoints(
    method: str, path: str, params: dict
) -> None:
    """Every persisting GET endpoint must 401 when workspace_id is absent.

    The guard runs in the router layer (not the service), so this never
    reaches the asyncpg NotNullViolationError on workspace_id NOT NULL.
    """
    user = UserContext(user_id="42", project_id="proj-abc", workspace_id=None)
    app = _make_app(user)
    try:
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.request(method, path, params=params)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 401, (
        f"{path} returned {resp.status_code}, expected 401 "
        f"(detail: {resp.text!r})"
    )
    assert resp.json() == {"detail": "workspace_required"}


def test_ocr_region_post_missing_workspace_returns_401() -> None:
    """POST /pdf/ocr_region must 401 when workspace_id is absent.

    Covers the only persisting POST endpoint in the PDF router. The bbox is
    valid (positive non-degenerate); the workspace_id guard must fire BEFORE
    the bbox validator so the user gets the workspace error, not bbox 422.
    """
    user = UserContext(user_id="42", project_id="proj-abc", workspace_id=None)
    app = _make_app(user)
    try:
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/pdf/ocr_region",
            json={
                "pdf_id": _VALID_PDF_ID,
                "page": 1,
                "bbox": [10.0, 20.0, 100.0, 200.0],
                "dpi": 300,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 401
    assert resp.json() == {"detail": "workspace_required"}


def test_workspace_id_non_uuid_returns_401() -> None:
    """A workspace_id that exists but is not a UUID must 401.

    Defense in depth: silver.pdf_*.workspace_id is typed uuid; a JWT that
    smuggles a non-UUID string should be rejected at the router rather
    than crashing asyncpg on the cast.
    """
    user = UserContext(
        user_id="42",
        project_id="proj-abc",
        workspace_id="not-a-uuid",
    )
    app = _make_app(user)
    try:
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(
            "/pdf/extract_text", params={"pdf_id": _VALID_PDF_ID, "page": 1}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 401
    assert resp.json() == {"detail": "workspace_required"}


def test_workspace_id_present_reaches_service_layer() -> None:
    """Sanity-check: a valid workspace_id passes the guard and is forwarded.

    Proves the guard isn't blocking legitimate traffic, and that the
    workspace_id is plumbed through to the service call as a uuid.UUID.
    """
    import uuid as _uuid

    workspace_str = "a0000000-0000-0000-0000-000000000001"
    user = UserContext(
        user_id="42",
        project_id="proj-abc",
        workspace_id=workspace_str,
    )
    app = _make_app_with_services(user)
    try:
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(
            "/pdf/extract_text",
            params={"pdf_id": _VALID_PDF_ID, "page": 1},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    service = app.state.pdf_extract_service
    service.extract_text.assert_awaited_once()
    kwargs = service.extract_text.await_args.kwargs
    assert kwargs["workspace_id"] == _uuid.UUID(workspace_str)
    assert kwargs["pdf_id"] == _VALID_PDF_ID
