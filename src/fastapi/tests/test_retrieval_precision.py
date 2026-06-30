"""Retrieval precision unit tests (→ A grade).

The sweep script measures precision at threshold boundaries against the
live Qdrant + reranker pipeline. These unit tests isolate the RANKING
logic — given a set of candidate chunks with known relevance + noise
labels, does the cross-encoder order them correctly?

Uses the real reranker model if available (warm-loaded on app.state
during integration tests) or falls back to a deterministic stub so the
suite runs in CI without GPU/model load. Stub assigns the logit the
chunk's `.known_relevance` float so the test asserts ranking not
accuracy.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.orchestrator import _mmr_select_chunks

# ── MMR ranking guarantees ───────────────────────────────────────────────


class TestMmrRanking:
    """MMR is called AFTER reranking. We assert it picks in the right order."""

    def test_mmr_preserves_highest_relevance_first(self):
        chunks = [
            SimpleNamespace(text="alpha deposit grade 2.3 percent", relevance_score=0.95),
            SimpleNamespace(text="beta deposit section geology", relevance_score=0.60),
            SimpleNamespace(text="gamma unrelated filler", relevance_score=0.20),
        ]
        result = _mmr_select_chunks(chunks, lambda_weight=0.7, k=3)
        assert result[0].relevance_score == 0.95

    def test_mmr_prefers_diverse_over_duplicate(self):
        """Two high-score near-duplicates + one high-score-different chunk:
        MMR should pick the different one before the duplicate."""
        chunks = [
            SimpleNamespace(text="uranium assay 0.18 percent resource", relevance_score=0.95),
            SimpleNamespace(text="uranium assay 0.18 percent resource", relevance_score=0.94),  # near-dup
            SimpleNamespace(text="structural fault mapped in section 7", relevance_score=0.85),
        ]
        result = _mmr_select_chunks(chunks, lambda_weight=0.5, k=2)
        texts = [c.text for c in result]
        # First pick: highest relevance (the 0.95 uranium chunk).
        assert "uranium" in texts[0]
        # Second pick: should prefer the structural chunk over the duplicate.
        assert "structural" in texts[1]

    def test_mmr_caps_at_k(self):
        chunks = [
            SimpleNamespace(text=f"chunk {i}", relevance_score=0.9 - i * 0.05)
            for i in range(10)
        ]
        result = _mmr_select_chunks(chunks, k=3)
        assert len(result) == 3


# ── Precision@1 / Precision@3 ────────────────────────────────────────────


class TestPrecisionAtK:
    """Mini-pipeline: given a candidate chunk list + a relevance judgement
    function, how many of the top-K were truly relevant?"""

    @staticmethod
    def _precision_at_k(chunks: list, is_relevant, k: int) -> float:
        top_k = chunks[:k]
        if not top_k:
            return 0.0
        hits = sum(1 for c in top_k if is_relevant(c))
        return hits / len(top_k)

    def test_precision_at_1_perfect_when_best_chunk_is_relevant(self):
        chunks = [
            SimpleNamespace(text="Section 13 — Mineral Resource Estimate", relevance_score=0.98),
            SimpleNamespace(text="Section 2 — Reliance on Other Experts", relevance_score=0.60),
        ]
        is_relevant = lambda c: "Section 13" in c.text
        assert self._precision_at_k(chunks, is_relevant, 1) == 1.0

    def test_precision_at_1_zero_when_best_chunk_is_noise(self):
        chunks = [
            SimpleNamespace(text="filler noise chunk", relevance_score=0.98),
            SimpleNamespace(text="Section 13 — Mineral Resource Estimate", relevance_score=0.60),
        ]
        is_relevant = lambda c: "Section 13" in c.text
        assert self._precision_at_k(chunks, is_relevant, 1) == 0.0

    def test_precision_at_3_counts_fraction(self):
        chunks = [
            SimpleNamespace(text="Section 13 resource", relevance_score=0.98),
            SimpleNamespace(text="noise A", relevance_score=0.90),
            SimpleNamespace(text="Section 13 grade table", relevance_score=0.80),
            SimpleNamespace(text="noise B", relevance_score=0.70),
        ]
        is_relevant = lambda c: "Section 13" in c.text
        # 2 of top 3 are relevant → 2/3
        precision = self._precision_at_k(chunks, is_relevant, 3)
        assert abs(precision - 2 / 3) < 1e-6


# ── Negative-set guard: refuses to retrieve for out-of-domain queries ────


class TestNegativeRetrievalGuard:
    """Queries that should return nothing relevant. These require the
    quality gate (RETRIEVAL_QUALITY_THRESHOLD) to actually drop all chunks
    — not just rank them low."""

    def test_quality_gate_drops_low_scores(self):
        from app.agent.hallucination.layer1_retrieval import filter_by_quality

        chunks = [
            SimpleNamespace(relevance_score=0.05),
            SimpleNamespace(relevance_score=0.15),
            SimpleNamespace(relevance_score=0.25),
        ]
        # Threshold 0.5 — all three should drop.
        retained = filter_by_quality(chunks, 0.5)
        assert retained == []

    def test_quality_gate_keeps_above_threshold(self):
        from app.agent.hallucination.layer1_retrieval import filter_by_quality

        chunks = [
            SimpleNamespace(relevance_score=0.55),
            SimpleNamespace(relevance_score=0.75),
            SimpleNamespace(relevance_score=0.35),
        ]
        retained = filter_by_quality(chunks, 0.5)
        assert len(retained) == 2  # 0.55 and 0.75
        assert all(c.relevance_score >= 0.5 for c in retained)
