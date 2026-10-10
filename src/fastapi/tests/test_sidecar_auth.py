"""Tests for the model-sidecar shared-secret auth + batch-size guards.

Audit 2026-06-27: the embedding/reranker/sparse sidecars had no
service-to-service auth and accepted unbounded request bodies.

Audit 2026-07-01: auth is now fail-CLOSED — an unset FASTAPI_SERVICE_KEY
refuses keyed routes with HTTP 503 unless SIDECAR_AUTH_OPTIONAL=true is set
explicitly. The client-side SERVICE_KEY_HEADERS falls back to
app.config.settings so .env-file-only deployments still authenticate.

Behaviour toggles are exercised via monkeypatch.setattr on the module globals
(auto-restored per test) rather than module reloads, so no auth state leaks
into later test modules. The two import-time header-resolution tests that do
need a reload restore the module in a ``finally``.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

import app.sidecar_auth as sidecar_auth

# ---------------------------------------------------------------------------
# require_service_key — enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_service_key_enforces_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "s3cr3t")
    # Correct key passes.
    await sidecar_auth.require_service_key(x_service_key="s3cr3t")
    # Wrong / missing key → 401.
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key="wrong")
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key=None)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_require_service_key_fails_closed_503_when_unset(monkeypatch) -> None:
    """Audit 2026-07-01: unset key must REFUSE (503), not silently skip."""
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "")
    monkeypatch.setattr(sidecar_auth, "_AUTH_OPTIONAL", False)
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key=None)
    assert ei.value.status_code == 503
    # Even a caller SENDING a key is refused — there is nothing to compare to.
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key="anything")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_explicit_opt_out_allows_unauthenticated(monkeypatch) -> None:
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "")
    monkeypatch.setattr(sidecar_auth, "_AUTH_OPTIONAL", True)
    # Explicit SIDECAR_AUTH_OPTIONAL → no-op, no exception.
    await sidecar_auth.require_service_key(x_service_key=None)
    await sidecar_auth.require_service_key(x_service_key="anything")


@pytest.mark.asyncio
async def test_non_ascii_header_is_401_not_500(monkeypatch) -> None:
    """Audit 2026-07-01: hmac.compare_digest on str raises TypeError for
    non-ASCII input (HTTP headers may carry latin-1) → was an unhandled 500.
    Bytes comparison must yield a clean 401."""
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "s3cr3t")
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key="clé-ключ-ø")
    assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# require_service_key — rotation overlap (FASTAPI_SERVICE_KEY_PREVIOUS)
#
# The sidecar read only FASTAPI_SERVICE_KEY, so a restart onto a rotated key
# 401'd every caller still on the old one — and the sparse task is a keyed
# sidecar on ECS. The main app has accepted the outgoing key since 2026-09-06
# (tests/test_service_key_previous.py); these pin the same behaviour here.
# ---------------------------------------------------------------------------

PRIMARY = "primary-key-0123456789abcdef0123456789abcdef"
PREVIOUS = "previous-key-0123456789abcdef0123456789abcdef"


@pytest.fixture
def rotating(monkeypatch):
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", PRIMARY)
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY_PREVIOUS", PREVIOUS)
    monkeypatch.setattr(sidecar_auth, "_previous_key_seen", False)


@pytest.mark.asyncio
async def test_previous_key_is_accepted_during_a_rotation(rotating) -> None:
    await sidecar_auth.require_service_key(x_service_key=PRIMARY)
    await sidecar_auth.require_service_key(x_service_key=PREVIOUS)
    for bad in ("wrong", None, ""):
        with pytest.raises(HTTPException) as ei:
            await sidecar_auth.require_service_key(x_service_key=bad)
        assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_previous_key_is_rejected_outside_a_rotation(monkeypatch) -> None:
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", PRIMARY)
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY_PREVIOUS", "")
    await sidecar_auth.require_service_key(x_service_key=PRIMARY)
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key=PREVIOUS)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_an_empty_previous_key_does_not_authenticate_an_empty_header(
    monkeypatch,
) -> None:
    """compare_digest(b"", b"") is True: "no rotation in progress" must not
    turn a missing header into a match."""
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", PRIMARY)
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY_PREVIOUS", "")
    for blank in (None, ""):
        with pytest.raises(HTTPException) as ei:
            await sidecar_auth.require_service_key(x_service_key=blank)
        assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_previous_key_does_not_stand_in_for_an_unset_primary(monkeypatch) -> None:
    """Fail-closed still wins: a previous key with no primary is a broken
    deployment, not a rotation."""
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "")
    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY_PREVIOUS", PREVIOUS)
    monkeypatch.setattr(sidecar_auth, "_AUTH_OPTIONAL", False)
    with pytest.raises(HTTPException) as ei:
        await sidecar_auth.require_service_key(x_service_key=PREVIOUS)
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_both_keys_are_compared_not_short_circuited(rotating, monkeypatch) -> None:
    """Matching the primary must still run the previous-key comparison, so the
    response time does not say which key the caller held."""
    from types import SimpleNamespace

    seen: list[tuple[bytes, bytes]] = []

    def spy(a: bytes, b: bytes) -> bool:
        seen.append((a, b))
        return a == b

    monkeypatch.setattr(sidecar_auth, "hmac", SimpleNamespace(compare_digest=spy))
    await sidecar_auth.require_service_key(x_service_key=PRIMARY)
    assert seen == [
        (PRIMARY.encode(), PRIMARY.encode()),
        (PRIMARY.encode(), PREVIOUS.encode()),
    ]


@pytest.mark.asyncio
async def test_previous_key_use_is_logged_once(rotating, caplog) -> None:
    with caplog.at_level("WARNING", logger=sidecar_auth.logger.name):
        await sidecar_auth.require_service_key(x_service_key=PREVIOUS)
        await sidecar_auth.require_service_key(x_service_key=PREVIOUS)
        await sidecar_auth.require_service_key(x_service_key=PRIMARY)
    hits = [r for r in caplog.records if "FASTAPI_SERVICE_KEY_PREVIOUS" in r.getMessage()]
    assert len(hits) == 1
    assert PREVIOUS not in hits[0].getMessage()


def test_rotation_overlap_over_http(rotating) -> None:
    """The dependency as a route sees it, header name and all."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.post("/sparse", dependencies=[Depends(sidecar_auth.require_service_key)])
    async def sparse() -> dict:
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/sparse", headers={"X-Service-Key": PRIMARY}).status_code == 200
    assert client.post("/sparse", headers={"X-Service-Key": PREVIOUS}).status_code == 200
    assert client.post("/sparse", headers={"X-Service-Key": "wrong"}).status_code == 401
    assert client.post("/sparse").status_code == 401


def test_previous_key_is_read_from_the_environment(monkeypatch) -> None:
    try:
        monkeypatch.setenv("FASTAPI_SERVICE_KEY", PRIMARY)
        monkeypatch.setenv("FASTAPI_SERVICE_KEY_PREVIOUS", f"  {PREVIOUS}\n")
        mod = importlib.reload(sidecar_auth)
        assert mod._SERVICE_KEY == PRIMARY
        assert mod._SERVICE_KEY_PREVIOUS == PREVIOUS  # stripped, like the primary
        # ...and the CLIENT side still sends only the primary.
        assert mod.SERVICE_KEY_HEADERS == {"X-Service-Key": PRIMARY}
    finally:
        monkeypatch.undo()
        importlib.reload(sidecar_auth)


def test_previous_key_defaults_to_none_in_progress(monkeypatch) -> None:
    try:
        monkeypatch.delenv("FASTAPI_SERVICE_KEY_PREVIOUS", raising=False)
        mod = importlib.reload(sidecar_auth)
        assert mod._SERVICE_KEY_PREVIOUS == ""
    finally:
        monkeypatch.undo()
        importlib.reload(sidecar_auth)


# ---------------------------------------------------------------------------
# SERVICE_KEY_HEADERS — client-side resolution (import-time; needs reloads)
# ---------------------------------------------------------------------------


def test_client_headers_from_env(monkeypatch) -> None:
    try:
        monkeypatch.setenv("FASTAPI_SERVICE_KEY", "env-key-abcdefghijklmnopqrstuvwxyz123456")
        mod = importlib.reload(sidecar_auth)
        assert mod.SERVICE_KEY_HEADERS == {
            "X-Service-Key": "env-key-abcdefghijklmnopqrstuvwxyz123456"
        }
        assert mod._SERVICE_KEY == "env-key-abcdefghijklmnopqrstuvwxyz123456"
    finally:
        monkeypatch.undo()
        importlib.reload(sidecar_auth)


def test_client_headers_fall_back_to_settings_when_env_unset(monkeypatch) -> None:
    """Audit 2026-07-01: the main service may get the key via pydantic-settings'
    .env file rather than process env; the client proxies must still send it.
    Server-side enforcement stays env-only (the sidecars can't import Settings).
    """
    from app.config import settings  # importable in the test env

    try:
        monkeypatch.delenv("FASTAPI_SERVICE_KEY", raising=False)
        mod = importlib.reload(sidecar_auth)
        assert {
            "X-Service-Key": settings.FASTAPI_SERVICE_KEY.strip()
        } == mod.SERVICE_KEY_HEADERS
        # Server-side key is env-only → unset here (fail-closed 503 behavior).
        assert mod._SERVICE_KEY == ""
    finally:
        monkeypatch.undo()
        importlib.reload(sidecar_auth)


# ---------------------------------------------------------------------------
# install_body_size_limit — chunked bodies are capped too
#
# The cap used to look at Content-Length alone, so a chunked body (no length
# header) or one that lied about its length sailed past it. FastAPI reads and
# parses the body BEFORE it resolves dependencies, i.e. before
# require_service_key runs, so an unauthenticated chunked POST was buffered in
# full: a straightforward way to OOM the sparse task, which holds the SPLADE++
# model in the same memory and has no hosted replacement to fail over to. The
# main app closed the same hole in API-8 (tests/test_router_audit_regressions.py);
# these pin it on the sidecars.
# ---------------------------------------------------------------------------


class _SparseReq(BaseModel):
    # Module level on purpose: under `from __future__ import annotations` FastAPI
    # resolves the route's annotations in module globals, so a class defined
    # inside the factory below would be read as a query parameter (422).
    texts: list[str]


def _sidecar_app(max_bytes: int):
    from fastapi import Depends, FastAPI

    app = FastAPI()
    sidecar_auth.install_body_size_limit(app, max_bytes=max_bytes)
    reached: list[int] = []

    @app.post("/sparse", dependencies=[Depends(sidecar_auth.require_service_key)])
    async def sparse(req: _SparseReq) -> dict:
        reached.append(len(req.texts))
        return {"n": len(req.texts)}

    app.state.reached = reached
    return app


def _json_chunks(n_chunks: int, chunk_bytes: int):
    """A JSON body delivered as a generator, i.e. with no Content-Length."""

    def gen():
        yield b'{"texts": ["'
        for _ in range(n_chunks):
            yield b"x" * chunk_bytes
        yield b'"]}'

    return gen()


def test_a_declared_length_over_the_cap_is_413(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "s3cr3t")
    app = _sidecar_app(1024)
    resp = TestClient(app).post(
        "/sparse", json={"texts": ["x" * 4096]}, headers={"X-Service-Key": "s3cr3t"}
    )
    assert resp.status_code == 413
    assert app.state.reached == []


def test_a_chunked_body_over_the_cap_is_413(monkeypatch) -> None:
    """No Content-Length: this is the one that got through."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "s3cr3t")
    app = _sidecar_app(1024)
    resp = TestClient(app).post(
        "/sparse",
        content=_json_chunks(64, 64),
        headers={"content-type": "application/json", "X-Service-Key": "s3cr3t"},
    )
    assert resp.status_code == 413
    assert app.state.reached == []


def test_an_unauthenticated_chunked_body_over_the_cap_is_413_not_401(monkeypatch) -> None:
    """The body is read before the key is checked, so the cap has to answer
    first -- that ordering is the whole reason it must cover chunked bodies."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "s3cr3t")
    resp = TestClient(_sidecar_app(1024)).post(
        "/sparse",
        content=_json_chunks(64, 64),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413


def test_a_body_that_lies_about_its_length_is_still_capped(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "s3cr3t")
    app = _sidecar_app(1024)
    body = b'{"texts": ["' + b"x" * 4096 + b'"]}'
    resp = TestClient(app).post(
        "/sparse",
        content=body,
        headers={
            "content-type": "application/json",
            "content-length": "10",
            "X-Service-Key": "s3cr3t",
        },
    )
    assert resp.status_code == 413
    assert app.state.reached == []


def test_a_chunked_body_under_the_cap_passes(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(sidecar_auth, "_SERVICE_KEY", "s3cr3t")
    app = _sidecar_app(1024)
    resp = TestClient(app).post(
        "/sparse",
        content=_json_chunks(2, 16),
        headers={"content-type": "application/json", "X-Service-Key": "s3cr3t"},
    )
    assert resp.status_code == 200
    assert app.state.reached == [1]


@pytest.mark.asyncio
async def test_a_streamed_body_is_cut_off_at_the_cap_not_read_to_the_end() -> None:
    """Counting after the body is buffered would reject it and still have paid
    for it. The cap must stop pulling chunks once it is crossed."""
    app = _sidecar_app(1024)
    chunk, total = b"x" * 512, 200
    pulled = 0
    sent: list[dict] = []

    async def receive() -> dict:
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": chunk, "more_body": pulled < total}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/sparse",
        "raw_path": b"/sparse",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    assert pulled < total, f"read {pulled} of {total} chunks before refusing"
    assert app.state.reached == []


# ---------------------------------------------------------------------------
# enforce_batch_limits
# ---------------------------------------------------------------------------


def test_enforce_batch_limits_rejects_too_many_items() -> None:
    with pytest.raises(HTTPException) as ei:
        sidecar_auth.enforce_batch_limits(
            ["x"] * 11, max_items=10, max_total_chars=10_000, label="t"
        )
    assert ei.value.status_code == 413


def test_enforce_batch_limits_rejects_oversized_total() -> None:
    with pytest.raises(HTTPException) as ei:
        sidecar_auth.enforce_batch_limits(
            ["a" * 600, "b" * 600], max_items=100, max_total_chars=1000, label="t"
        )
    assert ei.value.status_code == 413


def test_enforce_batch_limits_allows_within_caps() -> None:
    # Should not raise.
    sidecar_auth.enforce_batch_limits(
        ["short", "inputs"], max_items=10, max_total_chars=10_000, label="t"
    )
