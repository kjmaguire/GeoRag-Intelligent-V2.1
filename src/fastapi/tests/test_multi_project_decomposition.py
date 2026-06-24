"""Tests for multi_project_decomposition — 2026-06-02.

Focused on the detection + sub-query generation logic. The DB lookup
(load_workspace_project_names) is intentionally not tested here since
it's a thin wrapper over an asyncpg.fetchrow.
"""

from __future__ import annotations

import pytest

from app.services.multi_project_decomposition import (
    DecompositionResult,
    build_per_project_sub_query,
    decompose_query,
    detect_projects_in_query,
)


PROJECTS = [
    "Shakespeare",
    "Ikkari",
    "Madsen",
    "Battle North",
    "WEST RED LAKE GOLD MINES  LTD",
    "Johnson",
]


class TestDetection:
    def test_single_token_project_matches(self):
        result = detect_projects_in_query(
            "What is the deepest hole in the Shakespeare Property?",
            PROJECTS,
        )
        assert result == ["Shakespeare"]

    def test_multiple_projects_detected(self):
        result = detect_projects_in_query(
            "Compare Shakespeare Property and Ikkari Project on permitting.",
            PROJECTS,
        )
        assert set(result) == {"Shakespeare", "Ikkari"}

    def test_three_projects_detected(self):
        result = detect_projects_in_query(
            "Among Shakespeare, Ikkari, and Madsen, which has the best grade?",
            PROJECTS,
        )
        assert set(result) == {"Shakespeare", "Ikkari", "Madsen"}

    def test_unknown_project_not_detected(self):
        # "Crackingstone" is not in the workspace project list — must not
        # appear in detected list.
        result = detect_projects_in_query(
            "What is the deepest hole in the Crackingstone Property?",
            PROJECTS,
        )
        assert result == []

    def test_multi_token_project_match(self):
        result = detect_projects_in_query(
            "How does Battle North compare to Ikkari?",
            PROJECTS,
        )
        assert set(result) == {"Battle North", "Ikkari"}

    def test_generic_single_tokens_ignored(self):
        # Single-token generic words like "mine", "project", "property"
        # must NOT count as project matches — they appear in almost
        # every geological query.
        result = detect_projects_in_query(
            "How many drill holes are in this project's mine?",
            ["Project", "Mine", "Property", "Ikkari"],
        )
        # Only Ikkari shouldn't match either since it's not in the query.
        assert result == []

    def test_case_insensitive(self):
        result = detect_projects_in_query(
            "what does the IKKARI project say about gold?",
            PROJECTS,
        )
        assert result == ["Ikkari"]

    def test_empty_inputs(self):
        assert detect_projects_in_query("", PROJECTS) == []
        assert detect_projects_in_query("anything", []) == []


class TestSubQueryBuilder:
    def test_strips_other_project_name(self):
        sub = build_per_project_sub_query(
            "Compare Madsen Mine vs Ikkari Project on grade.",
            "Madsen",
            ["Madsen", "Ikkari"],
        )
        # Sub-query for Madsen must NOT contain "Ikkari"
        assert "Ikkari" not in sub
        # Must mention Madsen
        assert "Madsen" in sub
        # Should reference the topic
        assert "grade" in sub.lower()

    def test_strips_compare_framing(self):
        sub = build_per_project_sub_query(
            "How do Shakespeare Property and Ikkari Project differ on permitting?",
            "Shakespeare",
            ["Shakespeare", "Ikkari"],
        )
        assert "Ikkari" not in sub
        # "differ" should be removed
        assert "differ" not in sub.lower()
        assert "permitting" in sub.lower()

    def test_appends_project_if_missing(self):
        sub = build_per_project_sub_query(
            "Tell me about grade.",
            "Madsen",
            ["Madsen"],
        )
        # Original had no project name; builder should append it.
        assert "Madsen" in sub


class TestDecomposeQuery:
    def test_no_decomposition_for_single_project(self):
        result = decompose_query(
            "What is the deepest hole in the Shakespeare Property?",
            PROJECTS,
        )
        assert isinstance(result, DecompositionResult)
        assert result.applied is False
        assert result.sub_queries == ()
        # Single project still detected even though decomposition skipped.
        assert result.detected_projects == ("Shakespeare",)

    def test_no_decomposition_without_comparative_framing(self):
        # 2026-06-02 — query mentions TWO projects but in a single-intent
        # framing (about Madsen's timeline, mentioning Pure Gold as the
        # prior operator). Decomposition would mis-split this; the
        # comparative-framing gate must catch it.
        result = decompose_query(
            "Which timeline detail in the report is captured by Pure Gold "
            "operated the mine from 2014 to 2023 before WRLG for Madsen Mine?",
            PROJECTS + ["Pure Gold", "PureGold"],
        )
        assert result.applied is False
        # Detection still finds the names — only the decomposition is skipped.
        assert len(result.detected_projects) >= 2

    def test_decomposition_fires_on_two_projects(self):
        result = decompose_query(
            "How do Shakespeare Property and Ikkari Project differ on permitting?",
            PROJECTS,
        )
        assert result.applied is True
        assert len(result.sub_queries) == 2
        assert set(result.detected_projects) == {"Shakespeare", "Ikkari"}
        # Each sub-query mentions only its focus project.
        for sq, focus in zip(result.sub_queries, result.detected_projects):
            assert focus in sq
            for other in PROJECTS:
                if other != focus and other in result.detected_projects:
                    assert other not in sq

    def test_all_queries_includes_original(self):
        result = decompose_query(
            "Compare Madsen Mine vs Ikkari Project on grade.",
            PROJECTS,
        )
        assert result.applied is True
        all_qs = result.all_queries()
        assert len(all_qs) == 3  # original + 2 sub-queries
        assert all_qs[0].startswith("Compare")

    def test_higher_min_projects_threshold(self):
        # Same query, but require 3+ projects → should not decompose.
        result = decompose_query(
            "How do Shakespeare Property and Ikkari Project differ?",
            PROJECTS,
            min_projects=3,
        )
        assert result.applied is False

    def test_empty_query(self):
        result = decompose_query("", PROJECTS)
        assert result.applied is False
        assert result.sub_queries == ()
