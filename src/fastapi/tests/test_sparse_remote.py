"""Shared-SPLADE-sidecar routing tests (app.services.sparse_encoder).

Pure: no model loads, no network — httpx + the gate var are monkeypatched.
"""
from __future__ import annotations

import pytest

from app.services import sparse_encoder as se


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def test_remote_encode_restores_int_keys(monkeypatch):
    import httpx

    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        # The sidecar returns JSON, which stringifies the int token-id keys.
        return _Resp({"sparse": [{"123": 0.5, "456": 1.25}]})

    monkeypatch.setattr(se, "SPARSE_SERVICE_URL", "http://sparse:8000/")
    monkeypatch.setattr(httpx, "post", fake_post)

    out = se._remote_encode_sparse(["uranium grade"])
    assert out == [{123: 0.5, 456: 1.25}]
    assert all(isinstance(k, int) for k in out[0])  # restored to int
    assert captured["url"] == "http://sparse:8000/sparse"
    assert captured["json"] == {"texts": ["uranium grade"]}


def test_encode_sparse_routes_remote_when_url_set(monkeypatch):
    monkeypatch.setattr(se, "SPARSE_SERVICE_URL", "http://sparse:8000")
    monkeypatch.setattr(se, "_remote_encode_sparse", lambda texts: [{7: 0.9} for _ in texts])
    # Single str → single dict (first element of the remote batch).
    assert se.encode_sparse("hole PLS-22-08") == {7: 0.9}


def test_encode_sparse_batch_routes_remote_when_url_set(monkeypatch):
    monkeypatch.setattr(se, "SPARSE_SERVICE_URL", "http://sparse:8000")
    monkeypatch.setattr(se, "_remote_encode_sparse", lambda texts: [{1: 1.0}] * len(texts))
    out = se.encode_sparse_batch(["a", "b", "c"])
    assert out == [{1: 1.0}, {1: 1.0}, {1: 1.0}]


def test_encode_sparse_uses_local_when_url_empty(monkeypatch):
    # URL empty → must take the LOCAL path, never the remote one. Prove it by
    # making the remote explode and the local model loader raise a sentinel.
    monkeypatch.setattr(se, "SPARSE_SERVICE_URL", "")

    def remote_boom(texts):
        raise AssertionError("must not route remote when SPARSE_SERVICE_URL is empty")

    monkeypatch.setattr(se, "_remote_encode_sparse", remote_boom)

    def local_marker():
        raise RuntimeError("LOCAL_PATH_TAKEN")

    monkeypatch.setattr(se, "_get_sparse_model", local_marker)

    with pytest.raises(RuntimeError, match="LOCAL_PATH_TAKEN"):
        se.encode_sparse("granite uranium")
