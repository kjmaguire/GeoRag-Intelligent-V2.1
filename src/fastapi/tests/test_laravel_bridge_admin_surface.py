"""Phase 2 — admin.surface_updated broadcast helper must NEVER cascade.

Same failure-swallowing contract as ``post_ingestion_progress`` and
``post_workspace_data_updated``: the durable record is whatever the
workflow committed; the broadcast is the latency optimisation that
lets admin pages re-fetch without manual refresh. A broadcast failure
must NOT fail the workflow.
"""
from __future__ import annotations

from app.services.laravel_bridge import post_admin_surface_updated


async def test_broadcast_swallows_unreachable_laravel(monkeypatch):
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    # Should not raise.
    await post_admin_surface_updated(
        surface="workflow-runs",
        affected_props=["workflow_runs"],
    )


async def test_broadcast_noop_when_service_key_missing(monkeypatch):
    monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")

    await post_admin_surface_updated(
        surface="ml-training",
        affected_props=["runs"],
    )


async def test_broadcast_handles_500_response(monkeypatch):
    import httpx

    async def _stub_post(self, url, json=None, headers=None):
        return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_admin_surface_updated(
        surface="reports",
        affected_props=["builds"],
    )


async def test_list_page_payload_shape(monkeypatch):
    """List-page POST: surface + affected_props, no surface_id."""
    import httpx

    captured: dict = {}

    async def _stub_post(self, url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers or {}
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_admin_surface_updated(
        surface="workflow-runs",
        affected_props=["workflow_runs"],
        payload={"workflow_kind": "score_targets", "status": "success"},
    )

    assert captured["url"].endswith("/api/internal/v1/admin-surface-updated")
    assert captured["json"] == {
        "surface": "workflow-runs",
        "affected_props": ["workflow_runs"],
        "payload": {"workflow_kind": "score_targets", "status": "success"},
    }
    assert "surface_id" not in captured["json"]
    assert captured["headers"].get("X-Service-Key") == "test-key-32-bytes-or-longer-for-validator-ok"


async def test_drilldown_payload_shape_includes_surface_id(monkeypatch):
    """Drilldown POST: surface_id passed through for per-resource channel routing."""
    import httpx

    captured: dict = {}

    async def _stub_post(self, url, json=None, headers=None):
        captured["json"] = json
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    run_id = "11111111-1111-1111-1111-111111111111"
    await post_admin_surface_updated(
        surface="target-run",
        surface_id=run_id,
        affected_props=["run"],
    )

    assert captured["json"]["surface"] == "target-run"
    assert captured["json"]["surface_id"] == run_id
    assert captured["json"]["affected_props"] == ["run"]
