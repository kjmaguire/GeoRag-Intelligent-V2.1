"""Eval 15 R3 follow-up — geological query expansion unit tests."""

from __future__ import annotations

import pytest

from app.services.geological_query_expansion import expand_query


class TestCommoditySymbols:
    def test_au_expands_to_gold(self) -> None:
        out = expand_query("Highest Au grade?")
        assert "Au (gold)" in out

    def test_au_does_not_match_inside_australia(self) -> None:
        # Word-boundary guard: "Au" must not match the "Au" inside
        # "Australia". Real customer case: regional context queries.
        out = expand_query("Drilling history in Australia?")
        assert "Au (gold)" not in out
        # Original text preserved.
        assert "Australia" in out

    def test_au_case_sensitive(self) -> None:
        # Lowercase "au" might be part of a French word or noise.
        # Only capitalised Au is the gold chemical symbol.
        out = expand_query("Send the document au revoir")
        assert "(gold)" not in out

    def test_cu_zn_pb_expand(self) -> None:
        out = expand_query("Cu/Zn/Pb ratios in the eastern zone")
        assert "Cu (copper)" in out
        assert "Zn (zinc)" in out
        assert "Pb (lead)" in out


class TestUnits:
    def test_g_per_t_expands(self) -> None:
        out = expand_query("Show me holes with > 12 g/t Au")
        assert "g/t (grams per tonne)" in out
        assert "Au (gold)" in out

    def test_ppm_expands(self) -> None:
        out = expand_query("Average Cu ppm in surface samples")
        assert "ppm (parts per million)" in out


class TestDrillingAbbreviations:
    def test_ddh_expands(self) -> None:
        out = expand_query("How many DDH were completed in 2022?")
        assert "DDH (diamond drillhole)" in out

    def test_rc_expands(self) -> None:
        out = expand_query("RC vs DDH costs")
        assert "RC (reverse circulation)" in out


class TestBudget:
    def test_max_expansions_cap(self) -> None:
        # Long abbreviation soup — verify we cap at 6 expansions to
        # avoid bloating the embedding input with redundant terms.
        out = expand_query(
            "Show Au Ag Cu Pb Zn Ni Co Mo Pt Pd grades in g/t and ppm"
        )
        # Count "(<word>)" annotations.
        annotation_count = out.count("(")
        assert annotation_count <= 6, (
            f"Got {annotation_count} expansions; budget is 6"
        )

    def test_each_abbreviation_expanded_at_most_once(self) -> None:
        out = expand_query("Au grade, plus Au follow-up sample Au")
        # Three mentions of Au — only the FIRST should be annotated.
        assert out.count("(gold)") == 1


class TestNoOp:
    def test_empty_query(self) -> None:
        assert expand_query("") == ""

    def test_query_with_no_abbreviations(self) -> None:
        original = "What is the deepest drillhole in this project?"
        assert expand_query(original) == original


# ---------------------------------------------------------------------------
# Audit 2026-10-04 item 13: hole ids are not mangled by the abbreviation table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hole_id", ["DDH-07", "RC-22-11", "AC-04", "RAB-12", "ddh-07"])
def test_hole_ids_survive_expansion_untouched(hole_id: str) -> None:
    from app.services.geological_query_expansion import expand_query

    out = expand_query(f"What did {hole_id} intersect?")
    assert hole_id in out
    assert f"({'diamond drillhole'})-" not in out
    assert "reverse circulation)-" not in out
    assert "air core)-" not in out
    assert "air blast)-" not in out


def test_abbreviations_outside_hole_ids_still_expand() -> None:
    from app.services.geological_query_expansion import expand_query

    out = expand_query("Au grade in DDH-07 over 3 g/t, and in a DDH program")
    assert "DDH-07" in out
    assert "Au (gold)" in out
    assert "g/t (grams per tonne)" in out
    # The free-standing "DDH" is not part of an id, so it is still annotated.
    assert "DDH (diamond drillhole) program" in out


def test_several_hole_ids_are_restored_in_order() -> None:
    from app.services.geological_query_expansion import expand_query

    out = expand_query("compare RC-22-11 with AC-04 and DDH-07")
    assert out.index("RC-22-11") < out.index("AC-04") < out.index("DDH-07")
