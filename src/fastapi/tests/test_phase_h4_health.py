"""Unit tests for the Phase H4 composite health router."""
from __future__ import annotations

import pytest

from app.routers import admin_tier234 as t


def test_phase_h4_health_router_mounted() -> None:
    assert t.phase_h4_health_router.prefix == "/api/v1/admin/phase-h4-health"


def test_phase_h4_health_in_module_all() -> None:
    assert "phase_h4_health_router" in t.__all__


def test_phase_h4_health_check_model() -> None:
    c = t.PhaseH4Check(name="pg_pool", ok=True)
    assert c.detail is None


def test_phase_h4_health_response_model_round_trips() -> None:
    from datetime import datetime
    h = t.PhaseH4Health(
        ok=False,
        checks=[
            t.PhaseH4Check(name="pg_pool", ok=False, detail="not initialised"),
        ],
        timestamp=datetime.now(),
    )
    dumped = h.model_dump()
    assert dumped["ok"] is False
    assert len(dumped["checks"]) == 1
    assert dumped["checks"][0]["detail"] == "not initialised"
