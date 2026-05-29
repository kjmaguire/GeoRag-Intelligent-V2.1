"""Unit tests for reranker_labels pure helpers — no Dagster, no DB, no vLLM.

Covers the three deterministic behaviours that must not silently drift
between runs:

  - sqrt-proportional stratified allocation (sample-size invariants)
  - leakage-ratio filter (regurgitation detector)
  - chunk_id determinism (so re-runs upsert the same row)

Also pins the train/val/test split-by-report behaviour because changing
the split function silently would invalidate every checkpoint trained
on the previous output.

Run with:
    pytest src/dagster/tests/test_reranker_labels_stratification.py -v
"""

from __future__ import annotations

import hashlib

import pytest

from georag_dagster.assets.reranker_labels_helpers import (
    CHECK_MAX_LEAKAGE_WARN_RATE,
    CHECK_MIN_TRIPLES,
    DOC_CLASSES,
    LEAKAGE_THRESHOLD,
    SOURCE_BUCKETS,
    compute_doc_class as _compute_doc_class,
    deterministic_chunk_id as _deterministic_chunk_id,
    leakage_ratio as _leakage_ratio,
    prompt_sha256 as _prompt_sha256,
    seed_from_run_id as _seed_from_run_id,
    sqrt_proportional_allocation as _sqrt_proportional_allocation,
    strata_key as _strata_key,
    train_val_test_split_by_report as _train_val_test_split_by_report,
)


# ---------------------------------------------------------------------------
# sqrt-proportional allocation
# ---------------------------------------------------------------------------

class TestSqrtAllocation:
    def test_empty_strata_returns_zero(self) -> None:
        result = _sqrt_proportional_allocation({}, target_total=100)
        assert result == {}

    def test_all_zero_counts_returns_zero(self) -> None:
        counts = {"a": 0, "b": 0}
        assert _sqrt_proportional_allocation(counts, target_total=100) == {"a": 0, "b": 0}

    def test_single_stratum_caps_at_population(self) -> None:
        # If target exceeds available, allocation is capped at population.
        counts = {"only": 50}
        result = _sqrt_proportional_allocation(counts, target_total=100)
        assert result == {"only": 50}

    def test_sum_approximately_target_when_capacity_allows(self) -> None:
        # 3 strata with comfortable headroom should sum near target.
        counts = {"a": 10_000, "b": 10_000, "c": 10_000}
        result = _sqrt_proportional_allocation(counts, target_total=300)
        assert sum(result.values()) == pytest.approx(300, abs=2)

    def test_sqrt_dampens_dominant_stratum(self) -> None:
        # Equal sqrt(weight) when counts are equal — exact thirds.
        counts = {"a": 1000, "b": 1000, "c": 1000}
        result = _sqrt_proportional_allocation(counts, target_total=300)
        assert result["a"] == result["b"] == result["c"] == 100

    def test_sqrt_weighting_vs_linear(self) -> None:
        # A 100x population stratum should only get a 10x larger sample under sqrt.
        counts = {"small": 100, "big": 10_000}
        result = _sqrt_proportional_allocation(counts, target_total=1100)
        ratio = result["big"] / result["small"]
        # sqrt(10000)/sqrt(100) = 10; tolerate rounding.
        assert 8 < ratio < 12

    def test_nine_strata_match_design(self) -> None:
        # Exercise the actual 9-stratum cross at realistic scale.
        counts = {
            _strata_key(b, c): 5_000 + i * 1_000
            for i, (b, c) in enumerate(
                (b, c) for b in SOURCE_BUCKETS for c in DOC_CLASSES
            )
        }
        result = _sqrt_proportional_allocation(counts, target_total=50_000)
        # Every stratum should receive at least some slots when populations are non-trivial.
        for k in counts:
            assert result[k] > 0
        # Total should not exceed target by much (rounding only).
        assert sum(result.values()) == pytest.approx(50_000, abs=10)


# ---------------------------------------------------------------------------
# leakage ratio
# ---------------------------------------------------------------------------

class TestLeakageRatio:
    def test_empty_chunk_returns_zero(self) -> None:
        assert _leakage_ratio("anything", "") == 0.0

    def test_full_copy_paste_has_high_leakage(self) -> None:
        chunk = (
            "The Smith deposit hosts disseminated chalcopyrite "
            "mineralisation within altered diorite. Average copper grade "
            "is reported at 0.42 percent across the resource estimate."
        )
        # Echoing the chunk verbatim → leakage should be well above threshold.
        leak = _leakage_ratio(chunk, chunk)
        assert leak > LEAKAGE_THRESHOLD
        assert leak >= 0.9

    def test_paraphrase_stays_under_threshold(self) -> None:
        chunk = (
            "The Smith deposit hosts disseminated chalcopyrite "
            "mineralisation within altered diorite. Average copper grade "
            "is reported at 0.42 percent across the resource estimate."
        )
        query = "What ore minerals occur in the host rock at this property?"
        assert _leakage_ratio(query, chunk) <= LEAKAGE_THRESHOLD

    def test_token_normalisation_ignores_punctuation(self) -> None:
        # "diorite." and "diorite" should be treated as the same token.
        chunk = "Mineralisation in altered diorite."
        query = "Mineralisation, diorite — what units host it?"
        leak = _leakage_ratio(query, chunk)
        # Both unique tokens ("mineralisation", "altered", "diorite")
        # > 3 chars, two of three overlap → ratio ~0.67.
        assert leak > 0.5


# ---------------------------------------------------------------------------
# chunk_id determinism
# ---------------------------------------------------------------------------

class TestChunkIdDeterminism:
    def test_same_inputs_produce_same_id(self) -> None:
        a = _deterministic_chunk_id("report-1", "ingest_extractions", 3, 0)
        b = _deterministic_chunk_id("report-1", "ingest_extractions", 3, 0)
        assert a == b

    def test_distinct_inputs_produce_distinct_ids(self) -> None:
        seen = {
            _deterministic_chunk_id("r", "t", p, region)
            for p in range(5)
            for region in range(5)
        }
        assert len(seen) == 25

    def test_ids_are_valid_uuids(self) -> None:
        import uuid

        cid = _deterministic_chunk_id("r", "t", 1, 0)
        # uuid.UUID raises on malformed input — round-trip is the assertion.
        assert str(uuid.UUID(cid)) == cid

    def test_table_name_affects_id(self) -> None:
        # The same (report, page, region) under different tables must be unique.
        a = _deterministic_chunk_id("r", "ingest_extractions", 1, 0)
        b = _deterministic_chunk_id("r", "ingest_ocr_results", 1, 0)
        assert a != b


# ---------------------------------------------------------------------------
# Seed / hash helpers
# ---------------------------------------------------------------------------

class TestSeedHelpers:
    def test_seed_is_deterministic(self) -> None:
        assert _seed_from_run_id("abc") == _seed_from_run_id("abc")

    def test_seed_differs_per_run_id(self) -> None:
        assert _seed_from_run_id("abc") != _seed_from_run_id("def")

    def test_prompt_sha256_is_hex_64(self) -> None:
        h = _prompt_sha256("system text", "user text")
        assert len(h) == 64
        assert int(h, 16) >= 0  # parseable as hex
        # Direct comparison against hashlib to pin the format.
        expected = hashlib.sha256(b"system text\n---\nuser text").hexdigest()
        assert h == expected


# ---------------------------------------------------------------------------
# Split-by-report
# ---------------------------------------------------------------------------

class TestSplitByReport:
    def test_split_is_deterministic_under_same_seed(self) -> None:
        rids = [f"r-{i}" for i in range(100)]
        a = _train_val_test_split_by_report(rids, seed=42)
        b = _train_val_test_split_by_report(rids, seed=42)
        assert a == b

    def test_split_changes_with_seed(self) -> None:
        rids = [f"r-{i}" for i in range(100)]
        a = _train_val_test_split_by_report(rids, seed=1)
        b = _train_val_test_split_by_report(rids, seed=2)
        assert a != b

    def test_split_roughly_matches_ratios(self) -> None:
        rids = [f"r-{i}" for i in range(1000)]
        assignment = _train_val_test_split_by_report(rids, seed=7)
        counts = {"train": 0, "val": 0, "test": 0}
        for v in assignment.values():
            counts[v] += 1
        assert counts["train"] == 800
        assert counts["val"] == 100
        assert counts["test"] == 100

    def test_no_report_appears_in_multiple_splits(self) -> None:
        rids = [f"r-{i}" for i in range(50)]
        a = _train_val_test_split_by_report(rids, seed=3)
        # Each report id has exactly one split assignment — by construction
        # of a dict, but assert the codomain is well-formed.
        assert set(a.keys()) == set(rids)
        assert set(a.values()) <= {"train", "val", "test"}


# ---------------------------------------------------------------------------
# Doc-class heuristic
# ---------------------------------------------------------------------------

class TestDocClassHeuristic:
    def test_drill_traces_wins(self) -> None:
        assert _compute_doc_class("Anything", has_drill_traces=True, has_samples=True) == "drill_log"

    def test_samples_when_no_drill(self) -> None:
        assert _compute_doc_class("X", has_drill_traces=False, has_samples=True) == "assay_table"

    def test_technical_report_title_is_ni43(self) -> None:
        assert _compute_doc_class(
            "Smith Project Technical Report 2024",
            has_drill_traces=False,
            has_samples=False,
        ) == "ni43"

    def test_unknown_title_defaults_to_ni43(self) -> None:
        assert _compute_doc_class("Random.pdf", has_drill_traces=False, has_samples=False) == "ni43"


# ---------------------------------------------------------------------------
# Asset-check thresholds — pin the contract so a casual edit can't
# weaken the gate without a deliberate test update.
# ---------------------------------------------------------------------------

class TestAssetCheckConstants:
    def test_minimum_triple_count(self) -> None:
        assert CHECK_MIN_TRIPLES == 150_000

    def test_max_leakage_warn_rate(self) -> None:
        assert CHECK_MAX_LEAKAGE_WARN_RATE == pytest.approx(0.05)
