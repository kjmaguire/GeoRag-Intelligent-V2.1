"""Phase 4 — public-geoscience tile invalidation bridge helper.

Same failure-swallowing contract as the Phase 1/2/3 helpers: durable
record is the upstream public_geo.* write; broadcast is the latency
optimisation. Failure must NEVER cascade.
"""
from __future__ import annotations

from app.services.laravel_bridge import post_public_geoscience_tiles_invalidated


async def test_swallows_unreachable_laravel(monkeypatch):
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    # Should not raise.
    await post_public_geoscience_tiles_invalidated(jurisdiction_epoch=1716578400)


async def test_noop_when_service_key_missing(monkeypatch):
    monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")

    await post_public_geoscience_tiles_invalidated(jurisdiction_epoch=42)


async def test_handles_500(monkeypatch):
    import httpx

    async def _stub_post(self, url, json=None, headers=None):
        return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_public_geoscience_tiles_invalidated(
        jurisdiction_epoch=42,
        source_ids=["pg_mines"],
    )


async def test_payload_shape_default(monkeypatch):
    """Without source_ids, the field is omitted (server treats absent as null)."""
    import httpx

    captured: dict = {}

    async def _stub_post(self, url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_public_geoscience_tiles_invalidated(jurisdiction_epoch=1716578400)

    assert captured["url"].endswith("/api/internal/v1/public-geoscience-tiles-invalidated")
    assert captured["json"]["jurisdiction_epoch"] == 1716578400
    assert "source_ids" not in captured["json"]


async def test_payload_shape_with_source_ids(monkeypatch):
    import httpx

    captured: dict = {}

    async def _stub_post(self, url, json=None, headers=None):
        captured["json"] = json
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_public_geoscience_tiles_invalidated(
        jurisdiction_epoch=99,
        source_ids=["smdi_deposits"],
    )

    assert captured["json"]["jurisdiction_epoch"] == 99
    assert captured["json"]["source_ids"] == ["smdi_deposits"]
