"""Tests for the opt-in Qwen3-Reranker causal-LM backend (audit 2026-06-28).

These do NOT load the real model — they verify the prompt formatting and the
yes/no-logit scoring math with a fake tokenizer + model, so the scoring
contract is pinned without a multi-GB download. The backend is opt-in
(RERANKER_BACKEND=qwen3_causal); bge-reranker-base remains the default.
"""

from __future__ import annotations

import pytest

from app.services.reranker import _Qwen3CausalReranker


def test_qwen3_format_includes_instruct_query_document() -> None:
    r = object.__new__(_Qwen3CausalReranker)
    r._instruction = "INSTR"
    out = r._format("the query", "the doc")
    assert out.startswith(_Qwen3CausalReranker._PREFIX)
    assert out.endswith(_Qwen3CausalReranker._SUFFIX)
    assert "<Instruct>: INSTR" in out
    assert "<Query>: the query" in out
    assert "<Document>: the doc" in out


def test_qwen3_predict_scores_pyes_from_logits() -> None:
    torch = pytest.importorskip("torch")

    r = object.__new__(_Qwen3CausalReranker)
    r._torch = torch
    r._device = "cpu"
    r._instruction = "i"
    r._max_length = 64
    r._batch_size = 8
    r._token_false = 0
    r._token_true = 1

    class _Enc(dict):
        def to(self, _device: str) -> "_Enc":
            return self

    def _fake_tokenizer(texts: list[str], **_kw: object) -> _Enc:
        # carry batch size so the fake model can shape its logits
        return _Enc(_n=len(texts))

    class _Out:
        def __init__(self, logits: object) -> None:
            self.logits = logits

    def _fake_model(_n: int = 1, **_kw: object) -> _Out:
        # logits [batch, seq=1, vocab=2]:
        #   pair 0 → strongly "yes" (token 1), pair 1 → strongly "no" (token 0)
        logits = torch.zeros((_n, 1, 2))
        if _n >= 1:
            logits[0, -1, 1] = 6.0
        if _n >= 2:
            logits[1, -1, 0] = 6.0
        return _Out(logits)

    r._tokenizer = _fake_tokenizer
    r._model = _fake_model

    scores = r.predict([("q", "relevant"), ("q", "irrelevant")])
    assert len(scores) == 2
    assert scores[0] > 0.9, "relevant pair should score high P(yes)"
    assert scores[1] < 0.1, "irrelevant pair should score low P(yes)"
