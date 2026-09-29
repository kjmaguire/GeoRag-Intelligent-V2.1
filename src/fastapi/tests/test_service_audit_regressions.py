"""Unit regressions for the 2026-09-29 FastAPI audit (API-5, 6, 9, 11, 12).

Unlike tests/test_router_audit_regressions.py these import modules that
build ``app.config.settings`` (or the Hatchet client) at import time, so
they need the dummy environment ci.yml's unit job provides
(FASTAPI_SERVICE_KEY, POSTGRES_PASSWORD, HATCHET_CLIENT_TOKEN).
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

APP_DIR = Path(__file__).resolve().parent.parent / "app"


# ---------------------------------------------------------------------------
# API-5 — every posture CRITICAL carries the one greppable token
# ---------------------------------------------------------------------------


def test_every_main_py_posture_critical_carries_the_marker() -> None:
    """deploy/aws/terraform/alerts.tf pages on the literal
    ``GEORAG_POSTURE_CRITICAL``; a CRITICAL without it pages nobody (the
    Azure-era catch-all alert this code used to rely on does not exist)."""
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert 'POSTURE_CRITICAL_MARKER = "GEORAG_POSTURE_CRITICAL"' in source

    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "critical"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_safety_logger"
    ]
    assert len(calls) >= 7  # 3 layer switches + 2 required-on + owner + scope + creds
    for call in calls:
        fmt = call.args[0]
        assert isinstance(fmt, ast.Constant) and fmt.value.startswith("%s "), ast.dump(fmt)
        first = call.args[1]
        assert isinstance(first, ast.Name) and first.id == "POSTURE_CRITICAL_MARKER"


# ---------------------------------------------------------------------------
# API-6 — one limiter, keyed per actor, probes exempt
# ---------------------------------------------------------------------------


def _request(headers: dict[str, str], path: str = "/x") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("10.0.0.5", 1234),
            "query_string": b"",
        }
    )


def _bearer(claims: dict[str, Any]) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"Bearer xx.{seg}.sig"


def test_rate_limit_key_prefers_jwt_then_workspace_header_then_ip() -> None:
    from app.services.rate_limit import workspace_user_key

    ws = "a0000000-0000-0000-0000-000000000001"
    assert (
        workspace_user_key(_request({"Authorization": _bearer({"workspace_id": ws, "sub": "7"})}))
        == f"ws:{ws}:user:7"
    )
    assert workspace_user_key(_request({"X-Workspace-Id": ws.upper()})) == f"ws:{ws}"
    # Not a UUID: must not mint a key per arbitrary header value.
    assert workspace_user_key(_request({"X-Workspace-Id": "anything"})) == "10.0.0.5"
    assert workspace_user_key(_request({})) == "10.0.0.5"


def test_default_limit_is_per_workspace_and_probes_are_exempt() -> None:
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded

    from app.services.rate_limit import ProbeExemptSlowAPIMiddleware, workspace_user_key

    app = FastAPI()
    app.state.limiter = Limiter(
        key_func=workspace_user_key, default_limits=["1/minute"], enabled=True
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/v1/shadow/ingest_pdf/trigger")
    async def trigger() -> dict[str, str]:
        return {"ok": "1"}

    async def _handler(request: Request, exc: RateLimitExceeded) -> PlainTextResponse:  # noqa: ARG001
        return PlainTextResponse("limited", status_code=429)

    app.add_exception_handler(RateLimitExceeded, _handler)  # type: ignore[arg-type]
    app.add_middleware(ProbeExemptSlowAPIMiddleware)
    client = TestClient(app)

    for _ in range(3):
        assert client.get("/health").status_code == 200

    ws_a = {"X-Workspace-Id": "a0000000-0000-0000-0000-00000000000a"}
    ws_b = {"X-Workspace-Id": "a0000000-0000-0000-0000-00000000000b"}
    path = "/internal/v1/shadow/ingest_pdf/trigger"
    assert client.post(path, headers=ws_a).status_code == 200
    # Same caller IP, different tenant: its own bucket, not a shared one.
    assert client.post(path, headers=ws_b).status_code == 200
    assert client.post(path, headers=ws_a).status_code == 429


def test_main_installs_the_actor_limiter_not_an_ip_keyed_one() -> None:
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert "key_func=get_remote_address" not in source
    assert "app.state.limiter = _actor_limiter" in source
    assert "ProbeExemptSlowAPIMiddleware" in source


def test_rate_limit_storage_uri_is_a_real_setting() -> None:
    from app.config import Settings

    assert "RATE_LIMIT_STORAGE_URI" in Settings.model_fields
    assert Settings.model_fields["RATE_LIMIT_STORAGE_URI"].default is None
    assert 'getattr(settings, "RATE_LIMIT_STORAGE_URI"' not in (
        APP_DIR / "services" / "rate_limit.py"
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# API-9 — integrations trigger: registry first, bounded key cache
# ---------------------------------------------------------------------------


def test_unknown_flow_is_404_before_any_key_lookup(monkeypatch) -> None:
    import app.routers.integrations_trigger as it

    async def _no_flow(_name: str) -> None:
        return None

    async def _must_not_run(*_a: Any, **_k: Any) -> None:
        raise AssertionError("key lookup ran for an unregistered flow")

    monkeypatch.setattr(it, "get_flow", _no_flow)
    monkeypatch.setattr(it, "averify_flow_jwt_token", _must_not_run)
    app = FastAPI()
    app.include_router(it.router)

    resp = TestClient(app).post(
        "/internal/v1/integrations/random-name-123/trigger",
        json={},
        headers={"Authorization": "Bearer a.b.c"},
    )

    assert resp.status_code == 404
    assert "Registered" not in resp.text


def test_flow_key_cache_prunes_expired_and_is_bounded() -> None:
    from app.services import flow_jwt

    flow_jwt._per_flow_cache.clear()
    try:
        flow_jwt._per_flow_cache["stale"] = ([], time.monotonic() - 10_000)
        flow_jwt._store_keys("fresh", [("kid", "secret")])
        assert "stale" not in flow_jwt._per_flow_cache
        assert flow_jwt._cached_keys("fresh") == [("kid", "secret")]

        for i in range(flow_jwt._PER_FLOW_CACHE_MAX + 50):
            flow_jwt._store_keys(f"flow-{i}", [])
        assert len(flow_jwt._per_flow_cache) <= flow_jwt._PER_FLOW_CACHE_MAX
    finally:
        flow_jwt._per_flow_cache.clear()


async def test_empty_flow_registry_is_cached_not_refetched(monkeypatch) -> None:
    """``_cache`` truthiness gated freshness, and an empty registry is the
    normal state — so every call opened a new DB connection."""
    from app.services import flow_registry

    calls = 0

    async def _fetch() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(flow_registry, "_fetch_rows", _fetch)
    monkeypatch.setattr(flow_registry, "_cache", {})
    monkeypatch.setattr(flow_registry, "_cache_loaded_at", 0.0)

    assert await flow_registry.get_flow("x") is None
    assert await flow_registry.get_flow("y") is None
    assert calls == 1


async def test_flow_registry_failure_backs_off(monkeypatch) -> None:
    from app.services import flow_registry

    calls = 0

    async def _boom() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        raise OSError("db down")

    monkeypatch.setattr(flow_registry, "_fetch_rows", _boom)
    monkeypatch.setattr(flow_registry, "_cache", {})
    monkeypatch.setattr(flow_registry, "_cache_loaded_at", 0.0)

    await flow_registry.get_registry()
    await flow_registry.get_registry()
    assert calls == 1


# ---------------------------------------------------------------------------
# API-11 — defaults
# ---------------------------------------------------------------------------


def test_openapi_docs_are_off_by_default() -> None:
    from app.config import Settings

    assert Settings.model_fields["OPENAPI_DOCS_PUBLIC"].default is False


def test_production_posture_flags_the_table_owner() -> None:
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert 'settings.POSTGRES_USER.strip() == "georag"' in source
    assert "min=4 max=25" not in source


# ---------------------------------------------------------------------------
# API-12 — the ingest-progress pool is created once and closed
# ---------------------------------------------------------------------------


async def test_progress_pool_is_created_once_under_concurrency(monkeypatch) -> None:
    from app.hatchet_workflows import _progress

    created: list[Any] = []

    class _FakePool:
        def __init__(self) -> None:
            self.closed = False

        def is_closing(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    async def _create_pool(*_a: Any, **_k: Any) -> _FakePool:
        await asyncio.sleep(0.01)
        pool = _FakePool()
        created.append(pool)
        return pool

    monkeypatch.setattr(_progress.asyncpg, "create_pool", _create_pool)
    monkeypatch.setattr(_progress, "_pool", None)

    pools = await asyncio.gather(*(_progress.get_pool() for _ in range(8)))

    assert len(created) == 1
    assert all(p is created[0] for p in pools)

    await _progress.close_pool()
    assert created[0].closed
    assert _progress._pool is None
    await _progress.close_pool()  # idempotent


def test_lifespan_closes_the_progress_pool() -> None:
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert "await _close_progress_pool()" in source
