"""What is and is not a drill-hole ID (2026-10-10 audit, findings 7, 8 and 9).

7. **The retrieval side read standards, intervals and years as holes.**
   `viz_builder.extract_hole_ids` matched the bare patterns with none of the
   masking the answer-side check had: "NI 43-101", "2011-2014", "120-126 m" and
   "pre-2010" all came back as hole IDs. A bogus ID filters an assay query to
   zero rows and forces the factual_lookup intent.
8. **Layer 4 reported ordinary word-digit tokens as fabricated holes.**
   HOLE_ID_RE is "two letters, a dash, digits" and matches "Pre-2010",
   "Zone-3", "Pb-206"; unless the token was in the evidence, Layer 4 said
   "Drill-hole ID ... not found in silver.collars" -- critical on its own, so
   it forced a retry and the confidence floor on a correct answer.
9. **Compact IDs were invisible.** "BH21", "DDH0023" and "SRE0912" have no dash,
   so neither side ever extracted them.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.agentic_retrieval import classify_intent_sync
from app.agent.agentic_retrieval.nodes import _hole_ids_from_query
from app.agent.hallucination.orchestrator_validators import verify_entities
from app.agent.hole_id_patterns import (
    find_lettered_hole_ids,
    iter_compact_hole_id_matches,
    iter_hole_id_matches,
)
from app.agent.multi_turn_resolver import extract_entity_mentions
from app.agent.viz_builder import extract_hole_ids

PROJECT = "5e2b8c1d-7a4f-4e39-b6d0-91c3a8f2e7b4"


def _compact(text: str) -> list[str]:
    return [m.group(1) for m in iter_compact_hole_id_matches(text)]


def _lettered(text: str) -> list[str]:
    return [m.group(1) for m in iter_hole_id_matches(text)]


# ---------------------------------------------------------------------------
# Finding 7 -- the retrieval side
# ---------------------------------------------------------------------------

#: The four queries of the audit, observed to give ['43-101'],
#: ['43-101', '2011-2014'], ['120-126'] and ['PRE-2010'].
NO_HOLE_QUERIES = [
    "What does the NI 43-101 report say about gold grades in the drill holes?",
    "According to the NI 43-101 report, how many drill holes were completed in 2011-2014?",
    "Which holes intersected more than 1% U3O8 between 120-126 m?",
    "How many holes were drilled pre-2010?",
]


class TestRetrievalSideExtraction:
    @pytest.mark.parametrize("query", NO_HOLE_QUERIES)
    def test_no_hole_is_named(self, query: str) -> None:
        assert extract_hole_ids(query) == []
        assert _hole_ids_from_query(query) == []

    @pytest.mark.parametrize("query", NO_HOLE_QUERIES)
    def test_the_intent_is_not_forced_to_a_collar_lookup(self, query: str) -> None:
        got = classify_intent_sync(query)
        assert "hole_id_detected" not in got.matched_triggers

    @pytest.mark.parametrize(
        "query",
        [
            "Show Post-2015 holes in Zone-3 and Lens-2",
            "Compare the mid-2019 programme with the Oct-2011 one for the drill holes",
            "What is the Pb-206 / U-238 age of the drill hole samples?",
            "Drill holes from the ISO-9001 audit in FY2021-22",
            "For the holes, what is the NAD-83 / UTM-13 position?",
        ],
    )
    def test_words_dates_standards_and_isotopes_are_not_holes(self, query: str) -> None:
        assert extract_hole_ids(query) == []

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("this hole please tell me about it, 36-1085", ["36-1085"]),
            ("Which holes intersected more than 1% U3O8 between 120-126 m in hole 36-1085?", ["36-1085"]),
            ("What did the NI 43-101 report say about hole PLS-22-08 in 2011-2014?", ["PLS-22-08"]),
            ("Compare DDH-1234 and BH-21", ["DDH-1234", "BH-21"]),
            ("assays for GH08-212 and SRE09-12", ["GH08-212", "SRE09-12"]),
            ("what is IC-11 and DH-2547", ["IC-11", "DH-2547"]),
            ("hole 3774-36-1458 please", ["3774-36-1458"]),
        ],
    )
    def test_a_real_hole_is_still_found(self, query: str, expected: list[str]) -> None:
        found = extract_hole_ids(query)
        for hole in expected:
            assert hole in found, found

    def test_a_hole_word_in_front_makes_an_odd_prefix_a_hole(self) -> None:
        """"CO" and "SUB" are prefixes of English words and of some hole series."""
        assert extract_hole_ids("assays for hole SUB-12 please") == ["SUB-12"]
        assert extract_hole_ids("assays for the CO-12 area") == []

    def test_the_numeric_tail_of_a_lettered_id_is_still_dropped_by_the_wrapper(self) -> None:
        assert _hole_ids_from_query("assays in hole PLS-22-08") == ["PLS-22-08"]

    def test_the_resolver_does_not_remember_a_word_as_a_hole(self) -> None:
        mentions = extract_entity_mentions(
            "Since pre-2010, Zone-3 and Lens-2 were drilled; hole PLS-22-08 is the deepest.", 1
        )
        assert [m.surface_form for m in mentions if m.entity_type == "hole"] == ["PLS-22-08"]


# ---------------------------------------------------------------------------
# Finding 9 -- compact IDs
# ---------------------------------------------------------------------------


class TestCompactHoleIds:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("What are the assays for BH21?", ["BH21"]),
            ("Show the collar of DDH0023", ["DDH0023"]),
            ("depth of RC045 and rc046", ["RC045", "RC046"]),
            ("tell me about hole SRE0912", ["SRE0912"]),
            ("holes SRE0912, SRE0913 and SRE0914 were logged", ["SRE0912", "SRE0913", "SRE0914"]),
            ("results for the drill hole xyz1234", ["XYZ1234"]),
        ],
    )
    def test_a_compact_id_is_extracted(self, query: str, expected: list[str]) -> None:
        assert extract_hole_ids(query) == expected

    @pytest.mark.parametrize(
        "query",
        [
            "What does NI43 require for a technical report?",
            "Is the U3O8 grade above 0.1%?",
            "Report the hole collars in WGS84",
            "Are the holes located in UTM13 or NAD83?",
            "Which holes use EPSG4326?",
            "Holes drilled in FY2021 under ISO9001",
            "The JORC2012 resource and the drill holes",
            "SRE0912",  # a bare token with no hole word and no drill-type prefix
            "CO2 and SO4 in the drill holes",
        ],
    )
    def test_standards_datums_and_formulas_are_not_holes(self, query: str) -> None:
        assert extract_hole_ids(query) == []

    def test_a_dashed_id_is_not_also_read_as_its_own_compact_head(self) -> None:
        assert extract_hole_ids("assays for SRE09-12") == ["SRE09-12"]
        assert extract_hole_ids("assays for GH08-212") == ["GH08-212"]
        assert _compact("hole GH08-212 and hole SRE09-12") == []

    def test_a_compact_spelling_of_a_dashed_hole_is_not_a_second_hole(self) -> None:
        assert extract_hole_ids("compare BH-21 with BH21") == ["BH-21"]

    def test_the_gate_reads_a_hole_word_shortly_before_the_token(self) -> None:
        assert _compact("hole SRE0912") == ["SRE0912"]
        far = "hole " + "x" * 40 + " SRE0912"
        assert _compact(far) == []


# ---------------------------------------------------------------------------
# Finding 8 -- Layer 4
# ---------------------------------------------------------------------------


def _canon(hole_id: str) -> str:
    return "".join(ch for ch in hole_id.upper() if ch.isalnum())


class _CollarPool:
    """silver.collars as the Layer 4 query sees it, holding ``stored`` holes."""

    def __init__(self, stored: list[str]) -> None:
        self.stored = stored
        self.calls: list[tuple[Any, ...]] = []

    def acquire(self):  # noqa: ANN201 -- asyncpg-shaped context manager
        pool = self

        class _Conn:
            async def fetch(self, sql: str, upper_ids: list[str], project_id: str, canon_ids: list[str]):
                pool.calls.append((sql, upper_ids, project_id, canon_ids))
                return [
                    {"hole_id": h, "hole_id_canonical": _canon(h)}
                    for h in pool.stored
                    if h.upper() in upper_ids or _canon(h) in canon_ids
                ]

        class _Acquire:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return _Acquire()


EVIDENCE = [("search_documents", {"chunks": [{"text": "Drilling resumed and returned 0.12% eU3O8."}]})]


def _critical(warnings: list[str]) -> list[str]:
    return [w for w in warnings if w.startswith("Layer 4: Drill-hole ID")]


class TestLayer4WordDigitTokens:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "token",
        ["Pre-2010", "Post-2015", "mid-2019", "Zone-3", "Lens-2", "Pb-206",
         "Oct-2011", "ISO-9001", "FY2021-22", "Sr-87", "Phase-2"],
    )
    async def test_an_ordinary_token_is_not_a_fabricated_hole(self, token: str) -> None:
        pool = _CollarPool([])
        warnings = await verify_entities(
            f"Results since {token} returned 0.12% eU3O8 [NI43-1].", PROJECT, pool, None, EVIDENCE,
        )
        assert _critical(warnings) == [], warnings
        assert not pool.calls, "nothing here is a hole, so the database is not asked"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hole", ["DDH-1234", "BH-21", "PLS-22-08", "GH08-212", "SRE09-12", "IC-11", "DH-2547"])
    async def test_a_fabricated_real_shaped_id_is_still_critical(self, hole: str) -> None:
        warnings = await verify_entities(
            f"Hole {hole} intersected 4.2 m at 8.1 g/t Au [DATA-1].", PROJECT, _CollarPool([]), None, EVIDENCE,
        )
        assert any(f"'{hole}'" in w for w in _critical(warnings)), warnings

    @pytest.mark.asyncio
    async def test_a_hole_word_in_front_keeps_the_check_on_an_odd_prefix(self) -> None:
        warnings = await verify_entities(
            "Hole SUB-3 intersected 4.2 m at 8.1 g/t Au [DATA-1].", PROJECT, _CollarPool([]), None, EVIDENCE,
        )
        assert any("'SUB-3'" in w for w in _critical(warnings)), warnings


class TestLayer4CompactIds:
    @pytest.mark.asyncio
    async def test_a_fabricated_compact_id_is_critical(self) -> None:
        for text in (
            "Hole DDH0099 returned 3.1 g/t Au over 2 m [DATA-1].",
            "BH21 returned 3.1 g/t Au over 2 m [DATA-1].",
            "Logging of hole SRE0912 returned 3.1 g/t Au over 2 m [DATA-1].",
        ):
            warnings = await verify_entities(text, PROJECT, _CollarPool([]), None, EVIDENCE)
            assert _critical(warnings), (text, warnings)

    @pytest.mark.asyncio
    async def test_a_real_hole_spelled_without_its_dash_resolves(self) -> None:
        collars = ("query_spatial_collars", {"count": 1, "collars": [{"hole_id": "BH-21", "total_depth": 152.0}]})
        pool = _CollarPool(["BH-21"])
        warnings = await verify_entities(
            "BH21 reached a total depth of 152 m [DATA-1].", PROJECT, pool, None, [collars],
        )
        assert pool.calls, "the compact id reaches the database"
        assert warnings == []

    @pytest.mark.asyncio
    async def test_standards_and_formulas_in_an_answer_are_not_holes(self) -> None:
        pool = _CollarPool([])
        warnings = await verify_entities(
            "Per NI43 and JORC2012, the U3O8 grade at the collars (WGS84, NAD83) is 0.12% [NI43-1].",
            PROJECT, pool, None, EVIDENCE,
        )
        assert _critical(warnings) == []
        assert not pool.calls


# ---------------------------------------------------------------------------
# the shared finders
# ---------------------------------------------------------------------------


class TestSharedFinders:
    def test_lettered_ids_keep_their_order_and_spelling(self) -> None:
        assert find_lettered_hole_ids("PLS-22-08, then Zone-3, then gh08-212") == ["PLS-22-08", "gh08-212"]

    @pytest.mark.parametrize("token", ["Pre-2010", "Zone-3", "Pb-206", "Oct-2011"])
    def test_excluded_tokens(self, token: str) -> None:
        assert _lettered(f"since {token}") == []

    @pytest.mark.parametrize("token", ["PB-206", "DDH-1234", "CO08-12", "ZRY-01", "XLS-24-01"])
    def test_series_written_like_a_hole_stay(self, token: str) -> None:
        """An isotope is written "Pb-206"; a hole series is capitals. And an
        embedded year digit ("CO08") makes the letters a company code."""
        assert _lettered(f"since {token}") == [token]
