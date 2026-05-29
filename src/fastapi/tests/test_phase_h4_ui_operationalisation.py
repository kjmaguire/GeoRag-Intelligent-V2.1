"""Smoke tests for the Phase H4 UI operationalisation pass:
  - TRG geojson endpoint surface
  - Report Builder per-section draft surface
  - Alerts inbox surface + acknowledge counter-row
  - Laravel bridge module shape
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.routers import admin_tier234 as t
from app.routers import report_builder as rb
from app.routers import target_recommendation_cockpit as trc


# ---------------------------------------------------------------------------
# Router-mount smoke tests
# ---------------------------------------------------------------------------
def test_alerts_router_mounted() -> None:
    assert t.alerts_router.prefix == "/api/v1/admin/alerts-inbox"


def test_alerts_router_in_module_all() -> None:
    assert "alerts_router" in t.__all__


def test_target_recommendation_geojson_route_present() -> None:
    """The new /runs/{run_id}/geojson endpoint must be registered."""
    routes = {r.path for r in trc.router.routes if hasattr(r, "path")}
    assert "/runs/{run_id}/geojson" in {
        p.removeprefix(trc.router.prefix) for p in routes if p.startswith(trc.router.prefix)
    }


def test_report_builder_put_section_route_present() -> None:
    """The new PUT /builds/{build_id}/sections/{section_id} must be registered."""
    paths = {r.path for r in rb.router.routes if hasattr(r, "path")}
    expected = rb.router.prefix + "/builds/{build_id}/sections/{section_id}"
    assert expected in paths


# ---------------------------------------------------------------------------
# Pydantic model contracts — alerts inbox
# ---------------------------------------------------------------------------
def test_alert_item_minimum_required_fields() -> None:
    """AlertItem must accept the minimum set returned by the SQL projection."""
    from datetime import datetime
    a = t.AlertItem(
        audit_id="3f1f2a32-1b8b-4f6a-9c2a-1234567890ab",
        action_type="cost.burn.alert",
        payload={"severity": "high", "spent_usd": 12.5},
        created_at=datetime(2026, 5, 14, 12, 0, 0),
    )
    assert a.action_type.endswith(".alert")
    assert a.acknowledged_at is None
    assert a.severity is None  # severity is derived from payload only by the route, not the model


def test_alert_item_rejects_missing_action_type() -> None:
    from datetime import datetime
    with pytest.raises(ValidationError):
        t.AlertItem(
            audit_id="3f1f2a32-1b8b-4f6a-9c2a-1234567890ab",
            payload={},
            created_at=datetime.now(),
        )  # type: ignore[call-arg]


def test_acknowledge_alert_model_requires_actor_and_audit_id() -> None:
    with pytest.raises(ValidationError):
        t.AcknowledgeAlert(audit_id="abc")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Pydantic model contracts — report builder section drafts
# ---------------------------------------------------------------------------
def test_section_draft_model_round_trip() -> None:
    """SectionDraft accepts a body markdown + optional updated_at/by."""
    sd = rb.SectionDraft(section_id="0001_intro", body_markdown="# Hello")
    assert sd.updated_at is None
    assert sd.updated_by_user_id is None


def test_section_draft_put_enforces_body_length_cap() -> None:
    """The 200_000 char limit is enforced at the Pydantic layer."""
    with pytest.raises(ValidationError):
        rb.SectionDraftPut(
            body_markdown="x" * 200_001,
            updated_by_user_id=1,
        )


def test_section_draft_put_minimum_valid() -> None:
    p = rb.SectionDraftPut(body_markdown="ok", updated_by_user_id=42)
    assert p.updated_by_user_id == 42


def test_build_envelope_default_drafts_is_empty_dict() -> None:
    from datetime import datetime
    env = rb.BuildEnvelope(
        build_id="abc",
        report_type="weekly_project_digest",
        workspace_id="w",
        project_id="p",
        requested_at=datetime.now(),
        sections_planned=0,
        sections=[],
    )
    assert env.drafts == {}
    assert env.status == "planned"


# ---------------------------------------------------------------------------
# laravel_bridge module shape
# ---------------------------------------------------------------------------
def test_laravel_bridge_exposes_post_report_build_progress() -> None:
    from app.services import laravel_bridge
    assert hasattr(laravel_bridge, "post_report_build_progress")
    assert callable(laravel_bridge.post_report_build_progress)


@pytest.mark.asyncio
async def test_laravel_bridge_no_service_key_is_noop(monkeypatch) -> None:
    """Without FASTAPI_SERVICE_KEY the bridge logs + returns; must not raise."""
    from app.services import laravel_bridge
    monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
    # No network call should be made; the function returns None.
    out = await laravel_bridge.post_report_build_progress(
        "00000000-0000-0000-0000-000000000000", "planning",
    )
    assert out is None


@pytest.mark.asyncio
async def test_laravel_bridge_network_failure_swallowed(monkeypatch) -> None:
    """A network failure must not propagate — broadcasts are best-effort."""
    from app.services import laravel_bridge
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "k")
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")  # closed port
    out = await laravel_bridge.post_report_build_progress(
        "00000000-0000-0000-0000-000000000000", "failed", message="boom",
    )
    assert out is None
