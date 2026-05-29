"""Regression tests pinning the §5e XL pre-flight 2026-05-29 decisions.

Locks Kyle's four answers from the pre-flight question round so a future
edit can't silently re-introduce the 2026-05-19 dataset's failure modes:

  1. Sample size — TARGET_SAMPLE_SIZE stays at 50_000 (large pool;
     downstream critique filter shapes the final keep rate)
  2. Short-chunk pre-filter — MIN_CHUNK_CHARS = 200, enforced at SQL
     extraction time so no vLLM call is wasted on page-numbers
  3. Multi-hop ratio — MULTI_HOP_RATIO_TARGET = 1.0 → pairs_per_report
     == chunks_per_report so single/multi-hop rows roughly balance
  4. (Not testable here — GPU strategy / pause vLLM is operational)
"""
from __future__ import annotations


def test_min_chunk_chars_locked_at_200():
    """Kyle's pre-flight decision: short-chunk filter at 200 characters.
    The 2026-05-19 run had `fact_span='3'` rows because page-numbers
    were getting through; 200 chars is the floor that prevents that."""
    from georag_dagster.assets.reranker_labels_helpers import MIN_CHUNK_CHARS

    assert MIN_CHUNK_CHARS == 200, (
        "MIN_CHUNK_CHARS was changed away from Kyle's pre-flight pick "
        "of 200. If this is intentional (e.g. you're shipping a v2 "
        "reranker with a different policy), update this test + bump "
        "PROMPT_VERSION + retire the prior dataset rows."
    )


def test_multi_hop_ratio_locked_at_1_0():
    """Kyle's pre-flight decision: 1:1 single-chunk vs multi-hop mix.
    The agentic-retrieval graph benefits from more multi-evidence
    training signal than the original 2:1 default provided."""
    from georag_dagster.assets.reranker_labels_helpers import (
        MULTI_HOP_RATIO_TARGET,
    )

    assert MULTI_HOP_RATIO_TARGET == 1.0, (
        "MULTI_HOP_RATIO_TARGET was changed away from Kyle's pre-flight "
        "pick of 1.0. This affects pairs_per_report in "
        "reranker_generated_queries — re-validate the training "
        "row balance before lowering."
    )


def test_document_passages_sql_enforces_min_chunk_chars():
    """ADR-0010 Session B: the chain now reads silver.document_passages
    via _FETCH_DOCUMENT_PASSAGES_SQL. The min-200-char filter is enforced
    at SQL time, not in Python. If a future edit changes the threshold
    or removes the filter, this test catches it before the next
    materialisation burns vLLM calls on page-numbers."""
    from georag_dagster.assets.reranker_labels import (
        _FETCH_DOCUMENT_PASSAGES_SQL,
    )
    from georag_dagster.assets.reranker_labels_helpers import MIN_CHUNK_CHARS

    assert f">= {MIN_CHUNK_CHARS}" in _FETCH_DOCUMENT_PASSAGES_SQL, (
        "_FETCH_DOCUMENT_PASSAGES_SQL is missing the `>= MIN_CHUNK_CHARS` "
        "filter. Without it, page-number / numeric-only chunks reach "
        "vLLM and produce placeholder queries."
    )


def test_document_passages_sql_targets_canonical_table():
    """ADR-0010 contract: the chunk-population asset MUST read from
    silver.document_passages, not the deprecated silver.ingest_extractions
    or silver.reports.sections_text. Hard-pin this so a future edit
    doesn't silently revert the canonical-source decision."""
    from georag_dagster.assets.reranker_labels import (
        _FETCH_DOCUMENT_PASSAGES_SQL,
    )

    assert "FROM silver.document_passages" in _FETCH_DOCUMENT_PASSAGES_SQL
    # Negative assertions — these tables are explicitly NOT the canonical
    # source per ADR-0010.
    assert "silver.ingest_extractions" not in _FETCH_DOCUMENT_PASSAGES_SQL
    assert "silver.ingest_ocr_results" not in _FETCH_DOCUMENT_PASSAGES_SQL
    assert "sections_text" not in _FETCH_DOCUMENT_PASSAGES_SQL


def test_document_passages_sql_skips_pending_reocr():
    """Rows whose OCR pass is still pending re-run should be excluded —
    their text is from a low-confidence first pass and will be replaced
    shortly. Indexing them in the training set would churn on the next
    reocr_complete materialisation."""
    from georag_dagster.assets.reranker_labels import (
        _FETCH_DOCUMENT_PASSAGES_SQL,
    )

    assert "pending_reocr" in _FETCH_DOCUMENT_PASSAGES_SQL


def test_mined_negatives_queries_canonical_collection():
    """ADR-0010 Session B: hard-neg mining must hit georag_chunks, not
    the legacy georag_reports. The module-level QDRANT_COLLECTION
    constant is the single switch."""
    from georag_dagster.assets.reranker_labels import QDRANT_COLLECTION

    assert QDRANT_COLLECTION == "georag_chunks", (
        f"QDRANT_COLLECTION={QDRANT_COLLECTION!r} — must be 'georag_chunks' "
        "per ADR-0010. Reverting to georag_reports breaks the canonical-"
        "source contract."
    )


def test_multi_hop_ratio_pair_count_math():
    """Sanity-check the per-report pair count formula.

    For C chunks at ratio R:
      target_pairs = max(1, round(C * R))
    At R = 1.0 this is just C (with the C=2 special-case → 1).
    """
    from georag_dagster.assets.reranker_labels_helpers import (
        MULTI_HOP_RATIO_TARGET,
    )

    def _pair_count(C: int, R: float = MULTI_HOP_RATIO_TARGET) -> int:
        if C < 2:
            return 0
        target = max(1, int(round(C * R)))
        if C == 2:
            return 1
        return target

    # C=2 has only one unique pair to draw — wrap-around would repeat it.
    assert _pair_count(2) == 1
    # C=3 → 3 pairs (sliding window with wrap: (0,1), (1,2), (2,0))
    assert _pair_count(3) == 3
    # C=10 at R=1.0 → 10 pairs
    assert _pair_count(10) == 10
    # C=50 at R=1.0 → 50 pairs (matches Kyle's "more multi-hop training")
    assert _pair_count(50) == 50
