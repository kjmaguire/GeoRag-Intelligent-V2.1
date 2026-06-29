"""Shared reranker sidecar — proxy + service tests (2026-06-24).

No model load, no network: the local CrossEncoder load and httpx are mocked.
Covers get_reranker_or_none()'s local-vs-remote routing, the _RemoteReranker
HTTP proxy, and the sidecar's /rerank + /health endpoints.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.services.reranker as rk


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeModel:
    """Stand-in CrossEncoder: predict returns a score per pair."""

    def predict(self, pairs):
        return [0.5 + 0.1 * i for i in range(len(pairs))]


# ---------------------------------------------------------------------------
# get_reranker_or_none routing
# ---------------------------------------------------------------------------

def test_returns_remote_proxy_when_service_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANKER_SERVICE_URL", "http://reranker:8000")
    # Must NOT load a local model in this path.
    monkeypatch.setattr(rk, "_get_reranker", lambda: (_ for _ in ()).throw(AssertionError("loaded locally")))
    r = rk.get_reranker_or_none()
    assert isinstance(r, rk._RemoteReranker)
    assert r._url == "http://reranker:8000/rerank"


def test_returns_local_singleton_when_service_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RERANKER_SERVICE_URL", raising=False)
    sentinel = _FakeModel()
    monkeypatch.setattr(rk, "_get_reranker", lambda: sentinel)
    assert rk.get_reranker_or_none() is sentinel


def test_returns_none_when_local_load_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RERANKER_SERVICE_URL", raising=False)
    monkeypatch.setattr(rk, "_get_reranker", lambda: (_ for _ in ()).throw(OSError("no model")))
    assert rk.get_reranker_or_none() is None


# ---------------------------------------------------------------------------
# _RemoteReranker.predict
# ---------------------------------------------------------------------------

def test_remote_predict_posts_pairs_and_parses_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, timeout, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        captured["headers"] = headers
        return _FakeResp({"scores": [0.9, 0.1]})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    proxy = rk._RemoteReranker("http://reranker:8000", timeout_s=7.0)
    scores = proxy.predict([("q", "passage one"), ("q", "passage two")])

    assert scores == [0.9, 0.1]
    assert captured["url"] == "http://reranker:8000/rerank"
    assert captured["json"] == {"pairs": [["q", "passage one"], ["q", "passage two"]]}
    assert captured["timeout"] == 7.0
    # Audit 2026-06-27: proxy now forwards the service-key header (empty dict
    # when FASTAPI_SERVICE_KEY is unset).
    assert captured["headers"] is not None


# ---------------------------------------------------------------------------
# sidecar app: /rerank + /health
# ---------------------------------------------------------------------------

def test_sidecar_rerank_returns_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.reranker_service as svc

    from app.sidecar_auth import SERVICE_KEY_HEADERS

    monkeypatch.setattr(svc, "_get_reranker", lambda: _FakeModel())
    with TestClient(svc.app) as client:
        resp = client.post(
            "/rerank", json={"pairs": [["q", "p1"], ["q", "p2"]]},
            headers=SERVICE_KEY_HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["scores"] == [0.5, 0.6]


def test_sidecar_rerank_empty_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.reranker_service as svc

    from app.sidecar_auth import SERVICE_KEY_HEADERS

    monkeypatch.setattr(svc, "_get_reranker", lambda: _FakeModel())
    with TestClient(svc.app) as client:
        resp = client.post(
            "/rerank", json={"pairs": []}, headers=SERVICE_KEY_HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["scores"] == []


def test_sidecar_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.reranker_service as svc

    monkeypatch.setattr(svc, "_get_reranker", lambda: _FakeModel())
    with TestClient(svc.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_sidecar_health_503_when_model_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.reranker_service as svc

    def _raise():
        raise OSError("model not loaded")

    monkeypatch.setattr(svc, "_get_reranker", _raise)
    with TestClient(svc.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 503
