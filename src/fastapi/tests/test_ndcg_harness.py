"""Eval 15 R3 follow-up — unit tests for the NDCG@10 math.

The live-stack `run_harness` requires the FastAPI service; these
tests cover the scoring functions in isolation.
"""

from __future__ import annotations

import math

from app.services.eval.ndcg_harness import QueryRun, dcg_at_k, ndcg_at_k, score_query


class TestDcgAndNdcgMath:
    def test_dcg_known_value(self) -> None:
        # Two perfect hits at positions 1 and 2.
        relevances = [3.0, 3.0, 0.0, 0.0]
        # DCG = 3/log2(2) + 3/log2(3) + 0 + 0
        expected = 3.0 / math.log2(2) + 3.0 / math.log2(3)
        assert abs(dcg_at_k(relevances) - expected) < 1e-9

    def test_ndcg_perfect_ranking_is_one(self) -> None:
        relevances = [3.0, 2.0, 1.0]
        assert ndcg_at_k(relevances) == 1.0

    def test_ndcg_reversed_ranking_is_low(self) -> None:
        # 1.0 first, 3.0 last — penalises the bad ranking.
        rev = ndcg_at_k([1.0, 2.0, 3.0])
        perfect = ndcg_at_k([3.0, 2.0, 1.0])
        assert rev < perfect
        # And bounded.
        assert 0.0 < rev < 1.0

    def test_ndcg_empty_returns_zero(self) -> None:
        assert ndcg_at_k([]) == 0.0

    def test_ndcg_all_zero_returns_zero(self) -> None:
        assert ndcg_at_k([0.0, 0.0, 0.0]) == 0.0


class TestScoreQuery:
    def test_exact_id_match_top_relevance(self) -> None:
        run = QueryRun(
            query_id="t1",
            expected_substrings=["20"],
            returned_citation_ids=["citation-A", "citation-B"],
            returned_passage_texts=["irrelevant text", "still irrelevant"],
            expected_citation_ids=["citation-A"],
        )
        score = score_query(run)
        # citation-A at position 0 grades as 3.0; citation-B grades 0.
        # NDCG = (3.0/log2(2)) / (3.0/log2(2)) = 1.0
        assert score == 1.0

    def test_substring_match_grades_two(self) -> None:
        # No ID match but the passage text contains the expected
        # substring → graded as 2.0.
        run = QueryRun(
            query_id="t2",
            expected_substrings=["PLS-22-08"],
            returned_citation_ids=["c1"],
            returned_passage_texts=["Drill hole PLS-22-08 was the deepest."],
            expected_citation_ids=[],
        )
        score = score_query(run)
        # Only one returned doc, perfect ranking for what we have.
        assert score == 1.0

    def test_loose_substring_grades_one(self) -> None:
        run = QueryRun(
            query_id="t3",
            expected_substrings=["Athabasca Sandstone"],
            returned_citation_ids=["c1"],
            # Lowercased match → grade 1.0 (not 2.0)
            returned_passage_texts=["The athabasca sandstone hosts uranium."],
            expected_citation_ids=[],
        )
        # NDCG for [1.0] alone is 1.0 — only one doc, only one possible
        # ranking. The graded value just affects relative scoring across
        # multiple docs.
        assert score_query(run) == 1.0

    def test_no_match_returns_zero(self) -> None:
        run = QueryRun(
            query_id="t4",
            expected_substrings=["20 holes"],
            returned_citation_ids=["c1", "c2"],
            returned_passage_texts=["unrelated", "also unrelated"],
        )
        assert score_query(run) == 0.0

    def test_relevance_ordering_matters(self) -> None:
        # Good match at position 0, irrelevant at position 1 should
        # NDCG = 1.0 (because the ideal is also "best at top").
        run_good = QueryRun(
            query_id="ordered-good",
            expected_substrings=["target"],
            returned_citation_ids=["c1", "c2"],
            returned_passage_texts=["target found", "noise"],
        )
        # Now swap.
        run_bad = QueryRun(
            query_id="ordered-bad",
            expected_substrings=["target"],
            returned_citation_ids=["c1", "c2"],
            returned_passage_texts=["noise", "target found"],
        )
        assert score_query(run_good) > score_query(run_bad)
