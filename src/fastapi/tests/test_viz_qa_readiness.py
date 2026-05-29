"""Integration tests for the §5.10 + §5.11 HTTP endpoints.

Endpoints under test:
  POST /v1/viz/qa         — Drillhole Visual QA (calls drillhole_visual_qa agent)
  POST /v1/viz/readiness  — Visual Readiness (calls visual_readiness agent)

The agents themselves are exercised by ``test_phase5_6_9_agents.py``
(pure-function unit tests). These tests cover the missing HTTP layer:
inventory builders, request validation, auth gating, and the JSON shape
the Laravel side consumes.

Integration tests — require the live FastAPI stack on localhost:8000
(per conftest.py). They run a fixed-known collar through the QA path
and assert the response shape; they don't depend on data state beyond
"there is at least one collar in the workspace".
"""
from __future__ import annotations

import httpx
import pytest

from tests.conftest import AUTH_HEADERS, FASTAPI_URL, TEST_WORKSPACE_ID

# A real collar in the test workspace. Created by phase0 Wyoming roll-front
# uranium seed. If you switch the test corpus, update this UUID.
LIVE_COLLAR_ID = "838582e4-4eee-4064-836f-a0dc7f6c2896"
LIVE_PROJECT_ID = "00000000-0000-0000-0000-000000000452"
NONEXISTENT_COLLAR_ID = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# POST /v1/viz/qa
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_viz_qa_known_collar_returns_200_with_inventory() -> None:
    """A real collar returns 200 + the §5.10 envelope with inventory keys."""
    with httpx.Client(base_url=FASTAPI_URL, headers=AUTH_HEADERS) as c:
        r = c.post("/v1/viz/qa", json={
            "collar_id":    LIVE_COLLAR_ID,
            "workspace_id": TEST_WORKSPACE_ID,
        }, timeout=10.0)

    assert r.status_code == 200, r.text
    body = r.json()

    # Envelope shape
    for key in ("outcome", "inventory", "qa"):
        assert key in body, f"missing '{key}' in {body}"

    # Inventory contract
    inv = body["inventory"]
    for key in (
        "has_collar", "has_total_depth", "has_azimuth_dip",
        "interval_count", "has_lithology_codes", "trace_point_count",
    ):
        assert key in inv, f"inventory missing '{key}'"
    assert inv["has_collar"] is True

    # QA contract
    qa = body["qa"]
    for key in ("visualization_ready", "issues", "supported_visualizations"):
        assert key in qa, f"qa missing '{key}'"
    assert isinstance(qa["issues"], list)
    assert isinstance(qa["supported_visualizations"], list)


@pytest.mark.integration
def test_viz_qa_unknown_collar_returns_has_collar_false() -> None:
    """A UUID with no matching row reports has_collar=False + critical issue."""
    with httpx.Client(base_url=FASTAPI_URL, headers=AUTH_HEADERS) as c:
        r = c.post("/v1/viz/qa", json={
            "collar_id":    NONEXISTENT_COLLAR_ID,
            "workspace_id": TEST_WORKSPACE_ID,
        }, timeout=10.0)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inventory"]["has_collar"] is False
    assert body["qa"]["visualization_ready"] is False
    assert any(
        i["severity"] == "critical" for i in body["qa"]["issues"]
    ), body["qa"]["issues"]


@pytest.mark.integration
def test_viz_qa_requires_authorization() -> None:
    """Hitting the endpoint without the Authorization header is rejected."""
    headers = {
        k: v for k, v in AUTH_HEADERS.items() if k != "Authorization"
    }
    with httpx.Client(base_url=FASTAPI_URL, headers=headers) as c:
        r = c.post("/v1/viz/qa", json={
            "collar_id": LIVE_COLLAR_ID,
        }, timeout=5.0)

    # extract_user_context raises 401 on missing JWT; FastAPI may surface
    # it as 401 or 403 depending on the middleware stack. Either is fine
    # for "the auth gate works".
    assert r.status_code in (401, 403), r.text


# ---------------------------------------------------------------------------
# POST /v1/viz/readiness
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_viz_readiness_strip_log_for_real_collar() -> None:
    """strip_log readiness against a real collar returns the §5.11 envelope."""
    with httpx.Client(base_url=FASTAPI_URL, headers=AUTH_HEADERS) as c:
        r = c.post("/v1/viz/readiness", json={
            "viz_kind":     "strip_log",
            "collar_id":    LIVE_COLLAR_ID,
            "workspace_id": TEST_WORKSPACE_ID,
        }, timeout=10.0)

    assert r.status_code == 200, r.text
    body = r.json()

    for key in ("outcome", "inventory", "readiness"):
        assert key in body, f"missing '{key}'"

    readiness = body["readiness"]
    for key in ("ready", "supported", "missing", "warnings"):
        assert key in readiness, f"readiness missing '{key}'"
    assert isinstance(readiness["ready"], bool)
    assert isinstance(readiness["missing"], list)


@pytest.mark.integration
def test_viz_readiness_stereonet_requires_collar_id() -> None:
    """stereonet without collar_id is rejected as 400."""
    with httpx.Client(base_url=FASTAPI_URL, headers=AUTH_HEADERS) as c:
        r = c.post("/v1/viz/readiness", json={
            "viz_kind":     "stereonet",
            "workspace_id": TEST_WORKSPACE_ID,
        }, timeout=10.0)

    assert r.status_code == 400, r.text
    assert "collar_id" in r.text


@pytest.mark.integration
def test_viz_readiness_cross_section_requires_project_id() -> None:
    """cross_section without project_id is rejected as 400."""
    with httpx.Client(base_url=FASTAPI_URL, headers=AUTH_HEADERS) as c:
        r = c.post("/v1/viz/readiness", json={
            "viz_kind":     "cross_section",
            "workspace_id": TEST_WORKSPACE_ID,
        }, timeout=10.0)

    assert r.status_code == 400, r.text
    assert "project_id" in r.text


@pytest.mark.integration
def test_viz_readiness_cross_section_for_real_project() -> None:
    """cross_section against a real project returns inventory + readiness."""
    with httpx.Client(base_url=FASTAPI_URL, headers=AUTH_HEADERS) as c:
        r = c.post("/v1/viz/readiness", json={
            "viz_kind":     "cross_section",
            "project_id":   LIVE_PROJECT_ID,
            "workspace_id": TEST_WORKSPACE_ID,
        }, timeout=10.0)

    assert r.status_code == 200, r.text
    body = r.json()
    inv = body["inventory"]
    for key in ("collar_count", "section_line_present", "interval_count"):
        assert key in inv, f"inventory missing '{key}'"


@pytest.mark.integration
def test_viz_readiness_unknown_viz_kind_rejected() -> None:
    """Pydantic Literal type rejects unknown viz_kind at request validation."""
    with httpx.Client(base_url=FASTAPI_URL, headers=AUTH_HEADERS) as c:
        r = c.post("/v1/viz/readiness", json={
            "viz_kind":     "long_section",  # not in the Literal
            "collar_id":    LIVE_COLLAR_ID,
            "workspace_id": TEST_WORKSPACE_ID,
        }, timeout=5.0)

    assert r.status_code == 422, r.text
