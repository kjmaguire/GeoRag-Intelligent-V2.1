"""Shared-embedding-sidecar proxy tests (app.services.embedding).

Pure: no model loads, no network — httpx is monkeypatched.
"""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from app.services import embedding as emb


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def test_remote_encode_single_returns_1d(monkeypatch):
    import httpx

    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"vectors": [[0.1, 0.2, 0.3]], "dimension": 3})

    monkeypatch.setattr(httpx, "post", fake_post)
    r = emb._RemoteEmbedding("http://enc:8000/")
    out = r.encode("hello", normalize_embeddings=True)

    assert isinstance(out, np.ndarray)
    assert out.shape == (3,)  # single str in → 1-D out, like SentenceTransformer
    assert out.tolist() == pytest.approx([0.1, 0.2, 0.3])
    assert captured["url"] == "http://enc:8000/embed"
    assert captured["json"] == {"sentences": ["hello"], "normalize": True}


def test_remote_encode_list_returns_2d(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "post",
        lambda url, json=None, timeout=None, headers=None: _Resp({"vectors": [[1.0, 2.0], [3.0, 4.0]], "dimension": 2}),
    )
    r = emb._RemoteEmbedding("http://enc:8000")
    out = r.encode(["a", "b"])  # no normalize kwarg → defaults False

    assert out.shape == (2, 2)
    assert out.tolist() == [[1.0, 2.0], [3.0, 4.0]]


def test_remote_dimension_is_fetched_then_cached(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp({"dimension": 1024}))
    r = emb._RemoteEmbedding("http://enc:8000")
    assert r.get_sentence_embedding_dimension() == 1024

    # Second call must use the cache, not the network.
    def boom(*a, **k):
        raise AssertionError("dimension should be cached after first fetch")

    monkeypatch.setattr(httpx, "get", boom)
    assert r.get_sentence_embedding_dimension() == 1024


def test_dimension_none_on_http_failure(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(httpx, "get", boom)
    r = emb._RemoteEmbedding("http://enc:8000")
    assert r.get_sentence_embedding_dimension() is None  # advisory — never raises


def test_get_embedding_model_routes_to_remote_when_url_set(monkeypatch):
    monkeypatch.setattr(emb, "EMBEDDING_SERVICE_URL", "http://enc:8000")
    m = emb.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")
    assert isinstance(m, emb._RemoteEmbedding)


def test_get_embedding_model_loads_local_cpu_when_unset(monkeypatch):
    monkeypatch.setattr(emb, "EMBEDDING_SERVICE_URL", "")
    called = {}

    fake_st = types.ModuleType("sentence_transformers")

    class FakeST:
        def __init__(self, name, device=None):
            called["name"] = name
            called["device"] = device

    fake_st.SentenceTransformer = FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    m = emb.get_embedding_model("some-model")
    assert isinstance(m, FakeST)
    assert called == {"name": "some-model", "device": "cpu"}  # local load stays CPU
