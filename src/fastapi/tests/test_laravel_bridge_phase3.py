"""Phase 3 — workspace.activity + user.inbox_updated broadcast helpers.

Same failure-swallowing contract as the Phase 1/2 helpers: durable record
is the DB write; broadcast is the latency optimisation. Failure must
NEVER cascade.
"""
from __future__ import annotations

from app.services.laravel_bridge import (
    post_user_inbox_updated,
    post_workspace_activity,
)

# ─── workspace.activity ─────────────────────────────────────────────────────


async def test_workspace_activity_swallows_unreachable_laravel(monkeypatch):
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_workspace_activity(
        workspace_id="11111111-1111-1111-1111-111111111111",
        affected_types=["projects", "kpis"],
    )


async def test_workspace_activity_noop_when_service_key_missing(monkeypatch):
    monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")

    await post_workspace_activity(
        workspace_id="11111111-1111-1111-1111-111111111111",
        affected_types=["cost"],
    )


async def test_workspace_activity_payload_shape(monkeypatch):
    import httpx

    captured: dict = {}

    async def _stub_post(self, url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_workspace_activity(
        workspace_id="22222222-2222-2222-2222-222222222222",
        affected_types=["projects"],
        payload={"verb": "created"},
    )

    assert captured["url"].endswith("/api/internal/v1/workspace-activity")
    assert captured["json"]["workspace_id"] == "22222222-2222-2222-2222-222222222222"
    assert captured["json"]["affected_types"] == ["projects"]
    assert captured["json"]["payload"] == {"verb": "created"}


# ─── user.inbox_updated ─────────────────────────────────────────────────────


async def test_user_inbox_updated_swallows_unreachable_laravel(monkeypatch):
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_user_inbox_updated(user_id=42, kind="mention")


async def test_user_inbox_updated_default_count_delta(monkeypatch):
    """Default count_delta=1 should land in the body."""
    import httpx

    captured: dict = {}

    async def _stub_post(self, url, json=None, headers=None):
        captured["json"] = json
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_user_inbox_updated(user_id=42, kind="mention")

    assert captured["json"]["user_id"] == 42
    assert captured["json"]["kind"] == "mention"
    assert captured["json"]["count_delta"] == 1


async def test_user_inbox_updated_handles_500(monkeypatch):
    import httpx

    async def _stub_post(self, url, json=None, headers=None):
        return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    # Should not raise even on 5xx.
    await post_user_inbox_updated(user_id=1, kind="refusal", count_delta=5)
