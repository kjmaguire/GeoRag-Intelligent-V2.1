"""Phase H4 Tier 2/3/4 admin router smoke tests."""
from __future__ import annotations

from datetime import datetime
import pytest

from app.routers import admin_tier234 as t


def test_rec_router_mounted() -> None:
    assert t.rec_router.prefix == "/api/v1/admin/recommendations"


def test_qp_router_mounted() -> None:
    assert t.qp_router.prefix == "/api/v1/admin/qp-credentials"


def test_workspace_members_router_mounted() -> None:
    assert t.ws_members_router.prefix == "/api/v1/admin/workspace-members"


def test_workspace_settings_router_mounted() -> None:
    assert t.ws_settings_router.prefix == "/api/v1/admin/workspace-settings"


def test_activepieces_channels_router_mounted() -> None:
    assert t.ap_router.prefix == "/api/v1/admin/activepieces-channels"


def test_audit_explorer_router_mounted() -> None:
    assert t.audit_explorer_router.prefix == "/api/v1/admin/audit-explorer"


def test_saved_maps_router_mounted() -> None:
    assert t.saved_maps_router.prefix == "/api/v1/admin/saved-maps"


def test_nbd_request_requires_at_least_one_gap() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        t.NbdRequest(
            workspace_id="11111111-1111-1111-1111-111111111111",
            project_id="22222222-2222-2222-2222-222222222222",
            evidence_gaps=[],
        )


def test_analogue_request_top_k_default() -> None:
    req = t.AnalogueRequest(
        workspace_id="11111111-1111-1111-1111-111111111111",
        target_model_id="athabasca_uranium",
        project_attributes={"commodities": ["uranium"]},
    )
    assert req.top_k == 10


def test_qp_create_validates_required_fields() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        t.QpCreate(user_id=1, name="", issuing_body="APGO", registration_number="x", jurisdiction="ON")


def test_workspace_setting_default_tone() -> None:
    s = t.WorkspaceSetting(workspace_id="11111111-1111-1111-1111-111111111111")
    assert s.default_tone == "technical"
    assert s.extra_payload == {}


def test_workspace_setting_put_default_tone() -> None:
    p = t.WorkspaceSettingPut()
    assert p.default_tone == "technical"


def test_ap_channel_put_default_active() -> None:
    p = t.ApChannelPut(webhook_url="https://example.com/webhook")
    assert p.is_active is True


@pytest.mark.asyncio
async def test_run_nbd_endpoint_returns_recommendations() -> None:
    """Smoke: invoke run_nbd against the live agent."""
    req = t.NbdRequest(
        workspace_id="11111111-1111-1111-1111-111111111111",
        project_id="22222222-2222-2222-2222-222222222222",
        evidence_gaps=["Need EM survey over conductive body."],
    )
    result = await t.run_nbd(req)
    assert "recommendations" in result
    assert "summary" in result


@pytest.mark.asyncio
async def test_run_analogue_endpoint_returns_analogues() -> None:
    req = t.AnalogueRequest(
        workspace_id="11111111-1111-1111-1111-111111111111",
        target_model_id="athabasca_uranium",
        project_attributes={
            "host_rocks": ["unconformity", "basement_graphitic"],
            "commodities": ["uranium"],
        },
    )
    result = await t.run_analogue(req)
    assert "analogues" in result
    assert "summary" in result
