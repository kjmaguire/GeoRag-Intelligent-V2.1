"""Phase 2b — workspace.data_updated broadcast helper must NEVER cascade.

Same failure-swallowing contract as ``post_ingestion_progress``: the
durable record is the targeting.* write the workflow already committed;
the broadcast is the latency optimisation that lets Foundry/Targets
re-fetch without a manual refresh. A broadcast failure must NOT fail
the workflow.
"""
from __future__ import annotations

from app.services.laravel_bridge import post_workspace_data_updated


async def test_broadcast_swallows_unreachable_laravel(monkeypatch):
    # Point at a closed port — connection refused.
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    # Should not raise.
    await post_workspace_data_updated(
        workspace_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        pipeline_run_id="00000000-0000-0000-0000-000000000003",
        affected_types=["targets"],
    )


async def test_broadcast_noop_when_service_key_missing(monkeypatch):
    """If FASTAPI_SERVICE_KEY isn't set, skip the call — symmetric to
    the ingestion helper's behaviour."""
    monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")

    await post_workspace_data_updated(
        workspace_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        pipeline_run_id="00000000-0000-0000-0000-000000000003",
        affected_types=["targets"],
    )


async def test_broadcast_handles_500_response(monkeypatch):
    """If Laravel returns 5xx, log + swallow."""
    import httpx

    async def _stub_post(self, url, json=None, headers=None):
        return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_workspace_data_updated(
        workspace_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        pipeline_run_id="00000000-0000-0000-0000-000000000003",
        affected_types=["targets", "reports"],
    )


async def test_broadcast_sends_expected_payload(monkeypatch):
    """Lock the wire shape: workspace_id, project_id, pipeline_run_id,
    affected_types — no extra fields, no renames. The Laravel bridge
    validator rejects unknowns / requires the canonical names."""
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

    await post_workspace_data_updated(
        workspace_id="11111111-1111-1111-1111-111111111111",
        project_id="22222222-2222-2222-2222-222222222222",
        pipeline_run_id="33333333-3333-3333-3333-333333333333",
        affected_types=["targets"],
    )

    assert captured["url"].endswith("/api/internal/v1/workspace-data-updated")
    assert captured["json"] == {
        "workspace_id": "11111111-1111-1111-1111-111111111111",
        "project_id": "22222222-2222-2222-2222-222222222222",
        "pipeline_run_id": "33333333-3333-3333-3333-333333333333",
        "affected_types": ["targets"],
    }
    assert captured["headers"].get("X-Service-Key") == "test-key-32-bytes-or-longer-for-validator-ok"
