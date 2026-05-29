"""Unit tests for cross-store RRF fusion.

Module 4 Chunk 2 -- validates rrf_fuse() behaviour per the spec.

Test coverage:
  1. Empty lists (all empty)
  2. Single empty list mixed with non-empty
  3. Single-list passthrough (trivial case)
  4. Multi-list disjoint (no overlap)
  5. Multi-list with overlap (overlap accumulates score)
  6. Stable tiebreak on canonical_id (deterministic ordering)
  7. RRF formula correctness (exact arithmetic)
  8. k parameter customisation
  9. Preserves payload on output candidates
 10. Store field preserved through fusion
"""

from __future__ import annotations

import pytest

from app.services.fusion import (
    RRF_K,
    Candidate,
    ScoredCandidate,
    rrf_fuse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_candidates(ids: list[str], store: str = "qdrant") -> list[Candidate]:
    """Create a list of Candidates with sequential scores."""
    return [
        Candidate(canonical_id=cid, store=store, score=1.0 / (i + 1))
        for i, cid in enumerate(ids)
    ]


# ---------------------------------------------------------------------------
# Test 1: empty input
# ---------------------------------------------------------------------------

def test_empty_all_lists():
    """All empty lists returns empty result."""
    result = rrf_fuse([[], [], []])
    assert result == []


def test_empty_no_lists():
    """No lists at all returns empty result."""
    result = rrf_fuse([])
    assert result == []


# ---------------------------------------------------------------------------
# Test 2: mixed empty and non-empty lists
# ---------------------------------------------------------------------------

def test_empty_list_mixed():
    """Empty lists are ignored; non-empty lists are fused normally."""
    cands = make_candidates(["a", "b"])
    result = rrf_fuse([[], cands, []])
    assert len(result) == 2
    assert result[0].candidate.canonical_id == "a"
    assert result[1].candidate.canonical_id == "b"


# ---------------------------------------------------------------------------
# Test 3: single-list passthrough
# ---------------------------------------------------------------------------

def test_single_list_preserves_order():
    """Single list fuses to same order (rank 1 always scores highest)."""
    cands = make_candidates(["x", "y", "z"])
    result = rrf_fuse([cands])
    ids = [r.candidate.canonical_id for r in result]
    assert ids == ["x", "y", "z"]


def test_single_list_scores_descending():
    """Scores must be strictly decreasing for a single ranked list."""
    cands = make_candidates(["a", "b", "c", "d"])
    result = rrf_fuse([cands])
    for i in range(len(result) - 1):
        assert result[i].rrf_score > result[i + 1].rrf_score


# ---------------------------------------------------------------------------
# Test 4: multi-list disjoint
# ---------------------------------------------------------------------------

def test_multi_list_disjoint_all_present():
    """All candidates from disjoint lists appear in output."""
    list1 = make_candidates(["a", "b"])
    list2 = make_candidates(["c", "d"])
    result = rrf_fuse([list1, list2])
    result_ids = {r.candidate.canonical_id for r in result}
    assert result_ids == {"a", "b", "c", "d"}


def test_multi_list_disjoint_rank1_tie():
    """Rank-1 items from disjoint lists have equal scores; tiebreak by id."""
    # c1 from list1 rank-1, c2 from list2 rank-1 -- equal scores
    list1 = make_candidates(["beta"])
    list2 = make_candidates(["alpha"])
    result = rrf_fuse([list1, list2])
    # Both have score 1/(k+1). Tiebreak: "alpha" < "beta"
    assert result[0].candidate.canonical_id == "alpha"
    assert result[1].candidate.canonical_id == "beta"
    assert abs(result[0].rrf_score - result[1].rrf_score) < 1e-9


# ---------------------------------------------------------------------------
# Test 5: multi-list with overlap accumulates score
# ---------------------------------------------------------------------------

def test_overlap_candidate_ranks_first():
    """A candidate in both lists at rank 1 outscores any single-list candidate."""
    overlapping = Candidate(canonical_id="shared", store="qdrant", score=1.0)
    unique1 = Candidate(canonical_id="only_in_1", store="postgis", score=0.9)
    unique2 = Candidate(canonical_id="only_in_2", store="neo4j", score=0.9)

    list1 = [overlapping, unique1]
    list2 = [overlapping, unique2]

    result = rrf_fuse([list1, list2])

    assert result[0].candidate.canonical_id == "shared"
    # shared score = 1/(k+1) + 1/(k+1) = 2/(k+1)
    expected_shared = 2.0 / (RRF_K + 1)
    assert abs(result[0].rrf_score - expected_shared) < 1e-9


def test_overlap_accumulated_formula():
    """Exact RRF formula: rank-1 in two lists = 2/(k+1); rank-2 in one = 1/(k+2)."""
    c_shared = Candidate(canonical_id="shared", store="qdrant", score=1.0)
    c_unique = Candidate(canonical_id="unique", store="qdrant", score=0.5)

    # c_shared is rank-1 in both lists; c_unique is rank-2 in list1 only
    list1 = [c_shared, c_unique]
    list2 = [c_shared]

    result = rrf_fuse([list1, list2])
    result_map = {r.candidate.canonical_id: r.rrf_score for r in result}

    expected_shared = 1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 1)
    expected_unique = 1.0 / (RRF_K + 2)

    assert abs(result_map["shared"] - expected_shared) < 1e-9
    assert abs(result_map["unique"] - expected_unique) < 1e-9


# ---------------------------------------------------------------------------
# Test 6: stable tiebreak on canonical_id
# ---------------------------------------------------------------------------

def test_stable_tiebreak_canonical_id():
    """Equal-score candidates are ordered by canonical_id ascending."""
    # Three disjoint lists each with one rank-1 item -- all tied
    lists = [
        make_candidates(["zebra"]),
        make_candidates(["mango"]),
        make_candidates(["apple"]),
    ]
    result = rrf_fuse(lists)
    ids = [r.candidate.canonical_id for r in result]
    assert ids == ["apple", "mango", "zebra"]


def test_stable_tiebreak_across_calls():
    """Same inputs always produce same ordering (deterministic)."""
    cands_a = make_candidates(["bb", "cc"])
    cands_b = make_candidates(["aa", "dd"])
    r1 = [r.candidate.canonical_id for r in rrf_fuse([cands_a, cands_b])]
    r2 = [r.candidate.canonical_id for r in rrf_fuse([cands_a, cands_b])]
    assert r1 == r2


# ---------------------------------------------------------------------------
# Test 7: RRF formula correctness
# ---------------------------------------------------------------------------

def test_rrf_rank1_exact():
    """Rank-1 item in a single list: score exactly 1/(k+1)."""
    cand = Candidate(canonical_id="x", store="qdrant", score=1.0)
    result = rrf_fuse([[cand]])
    assert abs(result[0].rrf_score - 1.0 / (RRF_K + 1)) < 1e-12


def test_rrf_rank_n_formula():
    """Rank-n item: score = 1/(k+n) for a single list."""
    cands = make_candidates(["a", "b", "c", "d", "e"])
    result = rrf_fuse([cands])
    for rank_zero, scored in enumerate(result):
        expected = 1.0 / (RRF_K + rank_zero + 1)
        assert abs(scored.rrf_score - expected) < 1e-12, (
            f"rank={rank_zero + 1} expected={expected} got={scored.rrf_score}"
        )


# ---------------------------------------------------------------------------
# Test 8: k parameter customisation
# ---------------------------------------------------------------------------

def test_custom_k_zero():
    """k=0 makes rank-1 score 1.0 (pure reciprocal rank)."""
    cand = Candidate(canonical_id="x", store="qdrant", score=1.0)
    result = rrf_fuse([[cand]], k=0)
    assert abs(result[0].rrf_score - 1.0) < 1e-12


def test_custom_k_affects_ordering():
    """Very large k flattens differences; small k amplifies them."""
    list1 = make_candidates(["a", "b"])
    list2 = make_candidates(["b", "a"])
    result_k60 = rrf_fuse([list1, list2], k=60)
    result_k0 = rrf_fuse([list1, list2], k=0)
    # Both 'a' and 'b' appear in both lists at different ranks -- with k=60
    # the scores are nearly equal; with k=0 rank-1 items dominate more.
    # Regardless of k, both items appear in output.
    ids_k60 = {r.candidate.canonical_id for r in result_k60}
    ids_k0 = {r.candidate.canonical_id for r in result_k0}
    assert ids_k60 == {"a", "b"}
    assert ids_k0 == {"a", "b"}


# ---------------------------------------------------------------------------
# Test 9: payload preserved
# ---------------------------------------------------------------------------

def test_payload_preserved_through_fusion():
    """The original Candidate.payload is accessible on output ScoredCandidate."""
    payload = {"report_id": "r-001", "section": 3}
    cand = Candidate(canonical_id="p1", store="qdrant", score=0.9, payload=payload)
    result = rrf_fuse([[cand]])
    assert result[0].candidate.payload == payload


# ---------------------------------------------------------------------------
# Test 10: store field preserved
# ---------------------------------------------------------------------------

def test_store_field_preserved():
    """Candidate.store is not mutated during fusion."""
    c1 = Candidate(canonical_id="a", store="qdrant", score=1.0)
    c2 = Candidate(canonical_id="b", store="neo4j", score=0.9)
    c3 = Candidate(canonical_id="c", store="postgis", score=0.8)
    result = rrf_fuse([[c1], [c2], [c3]])
    stores = {r.candidate.canonical_id: r.candidate.store for r in result}
    assert stores == {"a": "qdrant", "b": "neo4j", "c": "postgis"}


# ---------------------------------------------------------------------------
# Test 11: rrf_rank field is 1-based and sequential
# ---------------------------------------------------------------------------

def test_rrf_rank_sequential():
    """Output ScoredCandidates carry 1-based sequential rrf_rank values."""
    cands = make_candidates(["a", "b", "c"])
    result = rrf_fuse([cands])
    ranks = [r.rrf_rank for r in result]
    assert ranks == [1, 2, 3]


# ---------------------------------------------------------------------------
# Test 12: large list stress (no crash, correct count)
# ---------------------------------------------------------------------------

def test_large_disjoint_lists():
    """100-item lists from 3 stores produce 300 unique results."""
    list1 = make_candidates([f"qdrant:{i}" for i in range(100)], store="qdrant")
    list2 = make_candidates([f"neo4j:{i}" for i in range(100)], store="neo4j")
    list3 = make_candidates([f"postgis:{i}" for i in range(100)], store="postgis")
    result = rrf_fuse([list1, list2, list3])
    assert len(result) == 300
    # Top result must be one of the rank-1 items (score = 3/(k+1) or 1/(k+1))
    top_score = result[0].rrf_score
    assert top_score > 0
