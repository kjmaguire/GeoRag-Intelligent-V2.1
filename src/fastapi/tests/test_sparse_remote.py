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


class TestTheEmptyVectorWarningLeaksNothing:
    """OBS-16 — `encode_sparse` logged 80 characters of the input.

    Its two callers pass the user's expanded query (agent/tools.py) and
    document passage text (ingest/passage_embedder.py). Both are customer
    exploration data, and stdout here lands in ContainerAppConsoleLogs_CL
    with 30-day retention. `log_safe` exists to close exactly this leak;
    the embedding tree was never swept.

    The branch has not fired in the retained window, so this was latent
    rather than active — but it fires on symbol-heavy or non-Latin input,
    which is what an unusual property name or a coordinate string looks
    like.
    """

    def test_the_warning_does_not_carry_the_text(self) -> None:
        import inspect

        from app.services import sparse_encoder

        source = inspect.getsource(sparse_encoder)

        assert "text=%r" not in source, (
            "the raw-text warning is back; this is the leak log_safe exists "
            "to prevent"
        )
        assert "text[:80]" not in source

    def test_it_logs_a_correlatable_hash_instead(self) -> None:
        import inspect

        from app.services import sparse_encoder

        source = inspect.getsource(sparse_encoder)

        assert "query_hash(text)" in source, (
            "without a hash the log line cannot be tied back to the "
            "encrypted audit row, which is the whole point of replacing "
            "the excerpt rather than deleting it"
        )


class TestTextShape:
    """The diagnostic that replaces the excerpt.

    An empty SPLADE vector is a property of the input's character classes,
    not its meaning, so the shape summary keeps everything a debugger
    needs and none of what a reader must not see.
    """

    def test_it_reveals_no_content(self) -> None:
        from app.agent.log_safe import text_shape

        secret = "Fox Lake North, 578400E 6412300N, 4.2% U3O8"
        shape = text_shape(secret)

        for token in ("Fox", "Lake", "578400", "U3O8", "4.2"):
            assert token not in shape, token

    def test_it_distinguishes_the_inputs_that_produce_empty_vectors(
        self,
    ) -> None:
        from app.agent.log_safe import text_shape

        latin = text_shape("uranium grade at the north zone")
        symbols = text_shape("<<< >>> ||| ### @@@ %%%")
        cjk = text_shape("\u94c0\u77ff\u54c1\u4f4d")

        assert latin != symbols != cjk
        assert "nonascii=4" in cjk
        assert "alpha=0" in symbols

    def test_length_survives(self) -> None:
        from app.agent.log_safe import text_shape

        assert "len=11" in text_shape("hello world")

    def test_empty_and_none_are_distinguishable(self) -> None:
        from app.agent.log_safe import text_shape

        assert text_shape("") != text_shape(None)
        assert "len=0" in text_shape("")
        assert "len=0" in text_shape(None)

    def test_the_class_counts_add_up_to_the_length(self) -> None:
        """A summary whose parts do not sum to the whole is a summary
        someone will misread."""
        import re

        from app.agent.log_safe import text_shape

        sample = "PLS-22-08: 4.2% U3O8 \u00e9chantillon \u94c0 \t"
        shape = text_shape(sample)
        counts = {
            key: int(value)
            for key, value in re.findall(r"(alpha|digit|space|nonascii|other)=(\d+)", shape)
        }

        assert sum(counts.values()) == len(sample)
