"""Tests for the target sign-off agent retained by the cockpit router."""
from __future__ import annotations

import asyncio

import pytest

from app.agents.phase8.deposit_model import deposit_model
from app.agents.phase8.evidence_layer import evidence_layer
from app.agents.phase8.geologist_signoff import geologist_signoff


def _run(**kwargs):
    inner = getattr(geologist_signoff, "__wrapped__", geologist_signoff)
    return asyncio.run(inner(ctx=None, **kwargs))


def test_deposit_model_uranium_returns_athabasca() -> None:
    inner = getattr(deposit_model, "__wrapped__", deposit_model)
    result = asyncio.run(
        inner(ctx=None, workspace_id="ws", commodity_primary="uranium")
    )
    assert result["selected_slug"] == "athabasca_uranium"


def test_evidence_layer_emits_per_factor_layers() -> None:
    inner = getattr(evidence_layer, "__wrapped__", evidence_layer)
    result = asyncio.run(
        inner(
            ctx=None,
            workspace_id="ws",
            project_id="p",
            target_model_id="athabasca_uranium",
            aoi_geom_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        )
    )
    assert result["layers"]
    assert all(layer["factor_name"] for layer in result["layers"])


def test_signed_off_requires_verified_credential() -> None:
    with pytest.raises(ValueError):
        _run(
            workspace_id="ws",
            target_id="t1",
            qp_user_id=1,
            qp_credential_id="cred",
            decision="signed_off",
            rationale="ok",
            qp_signature_method="wet_signature",
            credential_verified=False,
        )


def test_signed_off_with_verification_succeeds() -> None:
    result = _run(
        workspace_id="ws",
        target_id="t1",
        qp_user_id=1,
        qp_credential_id="cred",
        decision="signed_off",
        rationale="ok",
        qp_signature_method="wet_signature",
        credential_verified=True,
    )
    assert result["decision"] == "signed_off"
    assert result["credential_verified_at"] is not None
    assert result["target_recommendations_hash"]


def test_rejected_does_not_require_verification() -> None:
    result = _run(
        workspace_id="ws",
        target_id="t1",
        qp_user_id=1,
        qp_credential_id="cred",
        decision="rejected",
        rationale="bad target",
        qp_signature_method="manual",
    )
    assert result["decision"] == "rejected"
