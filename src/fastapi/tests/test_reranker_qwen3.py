"""Tests for the Qwen3-Reranker causal-LM backend (audit 2026-06-28 / 2026-07-01).

These do NOT load the real model — they verify the prompt assembly, the
suffix-preserving truncation, and the yes/no LOG-ODDS scoring math with a fake
tokenizer + model, so the scoring contract is pinned without a multi-GB
download.

Contract (audit 2026-07-01):
  - predict() returns LOG-ODDS (logit_yes − logit_no), sign-preserving like a
    CrossEncoder logit — NOT a [0,1] probability. This keeps the
    RERANKER_SCORE_THRESHOLD=0.0 sign filter meaningful and makes the
    downstream sigmoid in tools.py produce the exact P(yes).
  - The chat-template SUFFIX must survive truncation of over-length documents
    (prefix/suffix ids are pre-tokenized and re-attached around the truncated
    middle) — otherwise the final position is not the yes/no decision point.
"""

from __future__ import annotations

import pytest

from app.services.reranker import _Qwen3CausalReranker


def test_qwen3_format_middle_includes_instruct_query_document() -> None:
    r = object.__new__(_Qwen3CausalReranker)
    r._instruction = "INSTR"
    out = r._format_middle("the query", "the doc")
    # The middle is scaffold-free: PREFIX/SUFFIX are attached as token ids in
    # _build_batch_input_ids so they survive truncation.
    assert _Qwen3CausalReranker._PREFIX not in out
    assert _Qwen3CausalReranker._SUFFIX not in out
    assert "<Instruct>: INSTR" in out
    assert "<Query>: the query" in out
    assert "<Document>: the doc" in out


class _StubTokenizer:
    """Char-per-token tokenizer stub honouring max_length truncation."""

    def __call__(
        self,
        texts,
        padding=False,
        truncation=None,
        max_length=None,
        add_special_tokens=False,
        return_attention_mask=False,
        **_kw,
    ):
        ids = [[ord(c) for c in t] for t in texts]
        if max_length is not None:
            ids = [row[:max_length] for row in ids]
        return {"input_ids": ids}


def test_qwen3_truncation_preserves_suffix_ids() -> None:
    """An over-length document must lose middle tokens, never the suffix."""
    r = object.__new__(_Qwen3CausalReranker)
    r._instruction = "i"
    # Budget must exceed the short pair's middle (~38 stub tokens) so only the
    # over-length document is truncated.
    r._max_length = 80
    r._tokenizer = _StubTokenizer()
    r._prefix_ids = [101, 102, 103]
    r._suffix_ids = [901, 902, 903]

    long_doc = "x" * 500  # far beyond the 40-token budget
    built = r._build_batch_input_ids([("q", long_doc), ("q", "short")])

    budget = r._max_length - len(r._prefix_ids) - len(r._suffix_ids)
    for ids in built:
        assert ids[:3] == [101, 102, 103], "prefix ids must lead every row"
        assert ids[-3:] == [901, 902, 903], (
            "suffix ids must survive truncation — the final position is the "
            "yes/no decision point"
        )
        assert len(ids) <= r._max_length
    # The over-length row consumed the full middle budget; the short one didn't.
    assert len(built[0]) == len(r._prefix_ids) + budget + len(r._suffix_ids)
    assert len(built[1]) < len(built[0])


def test_qwen3_predict_scores_log_odds_from_logits() -> None:
    torch = pytest.importorskip("torch")

    r = object.__new__(_Qwen3CausalReranker)
    r._torch = torch
    r._device = "cpu"
    r._instruction = "i"
    r._max_length = 64
    r._batch_size = 8
    r._token_false = 0
    r._token_true = 1
    r._prefix_ids = [7]
    r._suffix_ids = [8]

    class _Enc(dict):
        def to(self, _device: str) -> "_Enc":
            return self

    class _PadCapableTokenizer(_StubTokenizer):
        def pad(self, encoded, padding=True, return_tensors="pt", **_kw):
            n = len(encoded["input_ids"])
            return _Enc(_n=n)

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

    r._tokenizer = _PadCapableTokenizer()
    r._model = _fake_model

    scores = r.predict([("q", "relevant"), ("q", "irrelevant")])
    assert len(scores) == 2
    # Log-odds, not probabilities: sign-preserving, unbounded.
    assert scores[0] == pytest.approx(6.0), "relevant pair → positive log-odds"
    assert scores[1] == pytest.approx(-6.0), "irrelevant pair → negative log-odds"
    # The downstream sigmoid in tools.py recovers the exact P(yes) =
    # softmax([logit_no, logit_yes])[yes].
    import math

    p_yes = 1.0 / (1.0 + math.exp(-scores[0]))
    assert p_yes > 0.99


def test_qwen3_score_sign_matches_threshold_semantics() -> None:
    """RERANKER_SCORE_THRESHOLD=0.0 must mean 'net yes-evidence required'.

    A [0,1] probability output would make `score >= 0.0` a pass-everything
    gate (the audit 2026-07-01 regression this pins against).
    """
    torch = pytest.importorskip("torch")

    r = object.__new__(_Qwen3CausalReranker)
    r._torch = torch
    r._device = "cpu"
    r._instruction = "i"
    r._max_length = 64
    r._batch_size = 8
    r._token_false = 0
    r._token_true = 1
    r._prefix_ids = [7]
    r._suffix_ids = [8]

    class _Enc(dict):
        def to(self, _device: str) -> "_Enc":
            return self

    class _PadCapableTokenizer(_StubTokenizer):
        def pad(self, encoded, padding=True, return_tensors="pt", **_kw):
            return _Enc(_n=len(encoded["input_ids"]))

    class _Out:
        def __init__(self, logits: object) -> None:
            self.logits = logits

    def _model_leaning_no(_n: int = 1, **_kw: object) -> _Out:
        logits = torch.zeros((_n, 1, 2))
        logits[:, -1, 0] = 2.0  # every pair leans "no"
        return _Out(logits)

    r._tokenizer = _PadCapableTokenizer()
    r._model = _model_leaning_no

    scores = r.predict([("q", "weak passage")])
    assert scores[0] < 0.0, (
        "a no-leaning pair must score NEGATIVE so the 0.0 threshold drops it"
    )
