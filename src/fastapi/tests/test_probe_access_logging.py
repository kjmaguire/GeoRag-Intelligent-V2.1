"""Successful probe requests must not reach the log store; failing ones must.

fastapi-cc emitted 9,758 structured access-log lines for liveness and
readiness requests over two days -- ten percent of the tier's console
volume spent recording that nothing was wrong. The fix is a level change,
not a filter: a probe that SUCCEEDS logs at DEBUG and so is invisible at
the default level, while a probe that starts FAILING is the single most
useful line in the file and keeps its INFO.

That asymmetry is the whole point, so both halves are asserted here. A
plain "drop /health" filter would pass a test for the first half and
silently delete the evidence for the second.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from app.middleware import _PROBE_PATHS, StructuredAccessLogMiddleware


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(StructuredAccessLogMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        return JSONResponse({"status": "starting"}, status_code=503)

    @app.get("/query")
    async def query() -> dict[str, str]:
        return {"answer": "granodiorite"}

    return TestClient(app)


def _levels_for(caplog, path: str) -> list[int]:
    # Captured at the root, not by logger name: app/middleware/__init__.py
    # loads app/middleware.py under the module name "app._middleware_impl"
    # to work around the package shadowing the module, so the logger is
    # not called what the import path suggests. Records propagate either
    # way; naming it here would couple this test to that workaround.
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        _client().get(path)
    return [r.levelno for r in caplog.records if r.getMessage() == "request"]


def test_a_healthy_probe_is_logged_below_info(caplog) -> None:
    assert _levels_for(caplog, "/health") == [logging.DEBUG]


def test_a_failing_probe_keeps_its_info_line(caplog) -> None:
    # /ready answers 503 here. The status, not the path, decides.
    assert _levels_for(caplog, "/ready") == [logging.INFO]


def test_an_ordinary_request_is_unaffected(caplog) -> None:
    assert _levels_for(caplog, "/query") == [logging.INFO]


def test_the_probe_paths_cover_what_the_platform_actually_calls() -> None:
    # deploy/azure/containerapps/probes.json configures httpGet probes on
    # /health (hatchet worker) and /up (reverb, horizon); main.py serves
    # /health and /ready. Drifting apart means either dead entries here or
    # a live probe path still logging at INFO.
    assert {"/health", "/ready", "/up"} <= _PROBE_PATHS
