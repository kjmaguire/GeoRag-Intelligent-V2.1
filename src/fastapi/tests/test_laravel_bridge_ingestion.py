"""T7 of the reliability spec — Reverb broadcast failures must not
cascade into the DB write path.

The bridge swallows HTTP errors so that an unhealthy Laravel (or worse,
an unhealthy Reverb publisher behind Laravel) can't take down ingestion.
The DB row is the durable record; the broadcast is the latency
optimisation.
"""
from __future__ import annotations

from app.services.laravel_bridge import post_ingestion_progress


async def test_broadcast_swallows_unreachable_laravel(monkeypatch):
    # Point at a closed port — connection refused.
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    # Should not raise.
    await post_ingestion_progress(
        workspace_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        run_id="00000000-0000-0000-0000-000000000003",
        stage="parse",
        status="failed",
        message="connection refused test",
    )


async def test_broadcast_noop_when_service_key_missing(monkeypatch):
    """If FASTAPI_SERVICE_KEY isn't set we must skip the call entirely —
    otherwise the X-Service-Key header would be empty and Laravel would
    log spurious 401s.

    "Skip entirely" is asserted on the HTTP client: the helper swallows every
    failure, so with the old dead-port target a helper that dialled Laravel
    with no key passed as well."""
    import httpx

    calls: list[tuple[str, dict]] = []

    async def _stub_post(self, url, json=None, headers=None):
        calls.append((url, dict(headers or {})))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    args = dict(
        workspace_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        run_id="00000000-0000-0000-0000-000000000003",
        stage="parse",
        status="completed",
    )

    monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
    await post_ingestion_progress(**args)
    assert calls == [], f"the helper dialled Laravel without a service key: {calls}"

    # Control: with a key the same call goes out, carrying that key.
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")
    await post_ingestion_progress(**args)
    assert len(calls) == 1
    assert calls[0][1].get("X-Service-Key") == "test-key-32-bytes-or-longer-for-validator-ok"


async def test_report_build_progress_noop_when_service_key_missing(monkeypatch):
    """Same contract for the §15 report-build progress push, which had no key-missing test."""
    import httpx

    from app.services.laravel_bridge import post_report_build_progress

    calls: list[str] = []

    async def _stub_post(self, url, json=None, headers=None):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")

    monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
    await post_report_build_progress("00000000-0000-0000-0000-0000000000b1", "drafting")
    assert calls == [], f"the helper dialled Laravel without a service key: {calls}"

    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")
    await post_report_build_progress("00000000-0000-0000-0000-0000000000b1", "drafting")
    assert len(calls) == 1


async def test_broadcast_handles_500_response(monkeypatch):
    """If Laravel returns 5xx, we log + swallow."""
    import httpx

    async def _stub_post(self, url, json=None, headers=None):
        return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _stub_post)
    monkeypatch.setenv("LARAVEL_INTERNAL_URL", "http://laravel-stub:8000")
    monkeypatch.setenv("FASTAPI_SERVICE_KEY", "test-key-32-bytes-or-longer-for-validator-ok")

    await post_ingestion_progress(
        workspace_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        run_id="00000000-0000-0000-0000-000000000003",
        stage="persist",
        status="failed",
    )
