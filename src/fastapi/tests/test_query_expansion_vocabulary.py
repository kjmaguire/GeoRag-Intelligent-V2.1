"""The vocabulary gaps that mattered most on this corpus.

The expansion table was abbreviations-only and had no uranium entry at all --
on a platform whose flagship corpus is Cameco / Athabasca uranium material,
and whose own `identifier_boost._COMMODITY_CODES` lists both U and U3O8. It
also had no synonym for the terms geologists actually vary on: a user asks
about "grades", the passage says "assays"; a user asks about an "intercept",
the report calls it an "intersection".

So "What U3O8 grades did the Triple R zone return?" matched nothing and went
to the embedder unexpanded, leaving the dense branch to bridge
U3O8 -> uranium and grade -> assay unaided, and the sparse branch with no
shared full-word token to match on at all.
"""

from __future__ import annotations

import pytest

from app.services.geological_query_expansion import expand_query


def _max_paren_depth(text: str) -> int:
    depth = deepest = 0
    for char in text:
        if char == "(":
            depth += 1
            deepest = max(deepest, depth)
        elif char == ")":
            depth -= 1
    return deepest


class TestUranium:
    def test_the_headline_query_now_expands(self) -> None:
        out = expand_query("What U3O8 grades did the Triple R zone return?")

        assert "uranium oxide" in out
        assert "assays" in out

    def test_equivalent_uranium_is_recognised(self) -> None:
        assert "equivalent uranium oxide" in expand_query("eU3O8 assays for the holes")

    def test_bare_u_is_left_alone(self) -> None:
        """Ambiguous with the pronoun and with unit symbols."""
        out = expand_query("Can U tell me the depth?")

        assert "uranium" not in out.lower()


class TestSynonymGroups:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("Show me the best gold intercepts", "intersections"),
            ("What intersections were reported?", "intercept"),
            ("What was the true width?", "downhole width"),
            ("assays for the Athabasca holes", "grades"),
        ],
    )
    def test_a_term_carries_its_alternatives(self, query: str, expected: str) -> None:
        assert expected in expand_query(query)


class TestExpansionStaysReadable:
    @pytest.mark.parametrize(
        "query",
        [
            "What U3O8 grades did the Triple R zone return?",
            "Which DDH had the highest cut-off grade?",
            "What was the average Au grade and true width?",
        ],
    )
    def test_no_nested_parentheticals(self, query: str) -> None:
        """The groups are mutual -- grade offers assay, assay offers grade.

        A per-term loop re-scans the text it has just inserted, so "grades"
        became "grades (assays (grades) tenor)" and "cut-off grade"
        degenerated into four levels of parentheses, diluting the very
        embedding the expansion exists to sharpen.
        """
        assert _max_paren_depth(expand_query(query)) <= 1

    def test_a_query_with_nothing_to_expand_is_returned_unchanged(self) -> None:
        query = "How deep is the hole?"

        assert expand_query(query) == query

    def test_the_expansion_budget_is_respected(self) -> None:
        crowded = "Au Ag Cu Zn grade intercept true width DDH U3O8 assay"
        out = expand_query(crowded, max_expansions=3)

        assert out.count("(") == 3

    def test_empty_input(self) -> None:
        assert expand_query("") == ""
