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

from itertools import takewhile
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
        assert extract_hole_ids("assays for hole Zone-3 please") == ["ZONE-3"]
        assert extract_hole_ids("assays for hole Yr-2 please") == ["YR-2"]

    def test_the_numeric_tail_of_a_lettered_id_is_still_dropped_by_the_wrapper(self) -> None:
        assert _hole_ids_from_query("assays in hole PLS-22-08") == ["PLS-22-08"]

    def test_the_resolver_does_not_remember_a_word_as_a_hole(self) -> None:
        mentions = extract_entity_mentions(
            "Since pre-2010, Zone-3 and Lens-2 were drilled; hole PLS-22-08 is the deepest.", 1
        )
        assert [m.surface_form for m in mentions if m.entity_type == "hole"] == ["PLS-22-08"]


class TestSeriesThatAreAlsoWords:
    """"CO", "SUB", "MID", "PRE", "MAR", "ZONE" ... are English and hole series.

    The first fix for finding 8 excluded the prefixes outright, so a question
    about hole CO-12 named no hole at all and was routed as a synthesis query
    (2026-10-10 review, item 5). A prefix is now a date only when a YEAR follows
    it; a place name is told from a hole by its capitals (retrieval has no pool
    to ask; Layer 4 does).
    """

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("Show assays for CO-12", ["CO-12"]),
            ("What is the total depth of CO-12?", ["CO-12"]),
            ("Compare CO-12 and CO-13", ["CO-12", "CO-13"]),
            ("Show assays for holes CO-12 and CO-13", ["CO-12", "CO-13"]),
            ("Show assay results for SUB-3", ["SUB-3"]),
            ("grade in MID-1", ["MID-1"]),
            ("depth of PRE-12", ["PRE-12"]),
            ("show me MAR-12", ["MAR-12"]),
            ("OCT-1 collar", ["OCT-1"]),
            ("results for TARGET-4", ["TARGET-4"]),
            ("STOPE-12 assays", ["STOPE-12"]),
            ("BLOCK-12 intercepts", ["BLOCK-12"]),
            ("LINE-12 summary", ["LINE-12"]),
            ("ZONE-4 hole summary", ["ZONE-4"]),
            ("AREA-5 depth", ["AREA-5"]),
            ("GRID-4 dip", ["GRID-4"]),
        ],
    )
    def test_a_real_series_with_an_english_prefix_is_found(
        self, query: str, expected: list[str]
    ) -> None:
        assert extract_hole_ids(query) == expected

    @pytest.mark.parametrize(
        "query",
        [
            "pre-2010 drilling results",
            "post-2015 holes",
            "How many holes were drilled mid-2019?",
            "Oct-2011 program results",
            "Tell me about the Zone-3 vein",
            "The Lens-2 structure strikes north",
            "Phase-2 drilling results",
            "Which holes returned sub-3 g/t Au?",
            "Intervals of sub-10 m were excluded",
            "Yr-2 results",
            "ISO-9001 certified labs",
        ],
    )
    def test_a_date_a_place_or_a_figure_is_still_not_a_hole(self, query: str) -> None:
        assert extract_hole_ids(query) == []

    def test_a_question_about_such_a_hole_is_routed_as_a_hole_lookup(self) -> None:
        for query in ("Show assays for CO-12", "grade in MID-1", "ZONE-4 hole summary"):
            assert "hole_id_detected" in classify_intent_sync(query).matched_triggers, query
        for query in ("Tell me about the Zone-3 vein", "What are the pre-2010 drilling results?"):
            assert "hole_id_detected" not in classify_intent_sync(query).matched_triggers, query


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

    @pytest.mark.parametrize(
        "query",
        [
            "RC2012 program results",
            "What was found in the DD2021 campaign?",
            "The RC2020 program comprised 14 holes",
            "Hole depths are measured from the CGVD28 datum",
            "Hole (CGVD28 datum) elevations",
            "holes, NAVD88 elevations",
        ],
    )
    def test_a_program_year_or_a_datum_is_not_a_hole(self, query: str) -> None:
        """A drill-type prefix in front of a YEAR names a program; CGVD28 is a
        vertical datum. Neither is a hole, and "RC2012 program results" used to
        be routed as a collar lookup at confidence 1.0."""
        assert extract_hole_ids(query) == []
        assert "hole_id_detected" not in classify_intent_sync(query).matched_triggers

    def test_a_hole_word_makes_a_program_year_a_hole(self) -> None:
        assert extract_hole_ids("Hole RC2012 returned 5 g/t") == ["RC2012"]
        assert extract_hole_ids("holes RC2012, RC2013 and RC2014") == ["RC2012", "RC2013", "RC2014"]

    def test_other_drill_prefixed_numbers_stay_holes(self) -> None:
        assert extract_hole_ids("depth of AC1000") == ["AC1000"]
        assert extract_hole_ids("RC15 results") == ["RC15"]
        assert extract_hole_ids("results for DD02021") == ["DD02021"]

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

    @pytest.mark.parametrize(
        "text",
        [
            "hole SRE0912",
            "hole ID SRE0912",
            "hole No. SRE0912",
            "hole #SRE0912",
            "hole: SRE0912",
            "hole 'SRE0912'",
            "hole - SRE0912",
            "hole PLS-22-08 and SRE0912",
            "holes PLS-22-08, GH08-212 and SRE0912",
        ],
    )
    def test_ids_and_glue_between_a_hole_word_and_the_token_keep_it_a_hole(self, text: str) -> None:
        assert _compact(text) == ["SRE0912"]

    @pytest.mark.parametrize(
        "text",
        [
            "Hole PLS-22-08 (sample MS240301) returned 2.31 g/t Au",
            "Hole PLS-22-08 sample AB123456 returned 2.31 g/t Au",
            "the hole returned 2.31 g/t in sample CU123456",
            "drill hole 36-1085 and assay sample MS2024001",
            "hole PLS-22-08 was resampled as batch QA2024",
        ],
    )
    def test_a_sample_id_in_a_later_clause_is_not_a_hole(self, text: str) -> None:
        """The hole word is within 32 characters of the token, but a word that
        is not an ID stands between them: the sentence moved on."""
        assert _compact(text) == []
        sample_ids = {"MS240301", "AB123456", "CU123456", "MS2024001", "QA2024"}
        assert not sample_ids & set(extract_hole_ids(text))


# ---------------------------------------------------------------------------
# Finding 8 -- Layer 4
# ---------------------------------------------------------------------------


def _canon(hole_id: str) -> str:
    return "".join(ch for ch in hole_id.upper() if ch.isalnum())


class _CollarPool:
    """silver.collars as the Layer 4 queries see them, holding ``stored`` holes.

    ``calls`` records the hole lookups ``(sql, upper_ids, project_id,
    canon_ids)``; ``series_calls`` the questions "which of these letter
    prefixes begin a hole of this project?" ``(sql, project_id, prefixes)``.
    """

    def __init__(self, stored: list[str]) -> None:
        self.stored = stored
        self.calls: list[tuple[Any, ...]] = []
        self.series_calls: list[tuple[Any, ...]] = []

    def acquire(self):  # noqa: ANN201 -- asyncpg-shaped context manager
        pool = self

        class _Conn:
            async def fetch(self, sql: str, *args: Any):
                if "SELECT DISTINCT substring" in sql:
                    project_id, prefixes = args
                    pool.series_calls.append((sql, project_id, prefixes))
                    letters = {
                        "".join(takewhile(str.isalpha, h.upper())) for h in pool.stored
                    }
                    return [{"prefix": p} for p in prefixes if p in letters]
                upper_ids, project_id, canon_ids = args
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
        ["Pre-2010", "Post-2015", "mid-2019", "Pb-206", "Oct-2011", "ISO-9001", "FY2021-22",
         "Sr-87", "Phase-2", "Yr-2", "Figure-3"],
    )
    async def test_an_ordinary_token_is_not_a_fabricated_hole(self, token: str) -> None:
        pool = _CollarPool([])
        warnings = await verify_entities(
            f"Results since {token} returned 0.12% eU3O8 [NI43-1].", PROJECT, pool, None, EVIDENCE,
        )
        assert _critical(warnings) == [], warnings
        assert not pool.calls, "nothing here is a hole, so the database is not asked"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("token", ["Zone-3", "Lens-2", "ZONE-3", "Mar-12", "Target-4", "Stope-12"])
    async def test_a_place_or_a_month_is_asked_of_the_pool_and_is_no_hole_there(self, token: str) -> None:
        """"Zone-3" is a place -- unless this project drilled a ZONE series.
        The pool is asked, finds no hole of that series, and nothing is reported."""
        pool = _CollarPool(["PLS-22-08"])
        warnings = await verify_entities(
            f"Results from {token} returned 0.12% eU3O8 [NI43-1].", PROJECT, pool, None, EVIDENCE,
        )
        assert _critical(warnings) == [], warnings
        assert pool.series_calls, "the pool is asked whether the series exists"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("token", ["Zone-77", "ZONE-77", "Stope-99", "MAR-98"])
    async def test_a_place_in_a_project_that_drilled_that_series_is_a_fabricated_hole(
        self, token: str
    ) -> None:
        stored = ["ZONE-1", "ZONE-2", "STOPE-1", "MAR-12", "PLS-22-08"]
        warnings = await verify_entities(
            f"{token} returned 3.1 g/t Au over 2 m [DATA-1].", PROJECT, _CollarPool(stored), None, EVIDENCE,
        )
        assert any(f"'{token.upper()}'" in w for w in _critical(warnings)), warnings

    @pytest.mark.asyncio
    async def test_a_real_place_named_hole_resolves_without_a_warning(self) -> None:
        warnings = await verify_entities(
            "ZONE-2 returned 3.1 g/t Au over 2 m [DATA-1].", PROJECT,
            _CollarPool(["ZONE-1", "ZONE-2"]), None,
            [("query_spatial_collars", {"count": 1, "collars": [{"hole_id": "ZONE-2"}]})],
        )
        assert _critical(warnings) == [], warnings

    @pytest.mark.asyncio
    async def test_an_ambiguous_token_with_a_hole_word_is_certain(self) -> None:
        """"hole Zone-77" is a hole, whatever the project drilled."""
        warnings = await verify_entities(
            "Hole Zone-77 returned 3.1 g/t Au over 2 m [DATA-1].", PROJECT,
            _CollarPool(["PLS-22-08"]), None, EVIDENCE,
        )
        assert any("'ZONE-77'" in w for w in _critical(warnings)), warnings

    @pytest.mark.asyncio
    async def test_the_pool_is_asked_once_for_both_questions_on_one_connection(self) -> None:
        pool = _CollarPool(["ZONE-1"])
        await verify_entities(
            "Zone-3 and Zone-77 were drilled [DATA-1].", PROJECT, pool, None, EVIDENCE,
        )
        assert len(pool.calls) == 1 and len(pool.series_calls) == 1
        assert pool.series_calls[0][2] == ["ZONE"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "Samples below sub-3 g/t were re-assayed [NI43-1].",
            "Intervals of sub-10 m were excluded [NI43-1].",
            "Grades under zone-3 ppm are background [NI43-1].",
        ],
    )
    async def test_a_word_like_prefix_before_a_unit_is_a_figure(self, text: str) -> None:
        pool = _CollarPool([])
        warnings = await verify_entities(text, PROJECT, pool, None, EVIDENCE)
        assert _critical(warnings) == [], warnings
        assert not pool.calls and not pool.series_calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "CO-99 returned 3.1 g/t Au over 2 m [NI43-1].",
            "| CO-99 | 3.1 g/t |",
            "SUB-77 intersected 5 m of 2 g/t [NI43-1].",
            "MID-5 intersected 5 m of 2 g/t [NI43-1].",
            "PRE-9 intersected 5 m of 2 g/t [NI43-1].",
            "Holes CO-12 and CO-99 intersected mineralisation [NI43-1].",
        ],
    )
    async def test_a_fabricated_hole_with_an_english_prefix_is_critical(self, text: str) -> None:
        """"CO", "SUB", "MID", "PRE" are words and also hole series; only a YEAR
        behind them makes them a date. Silent on origin/main's successor."""
        warnings = await verify_entities(
            text, PROJECT, _CollarPool(["CO-12", "PLS-22-08"]), None, EVIDENCE
        )
        flagged = " ".join(_critical(warnings))
        assert any(token in flagged for token in ("CO-99", "SUB-77", "MID-5", "PRE-9")), warnings
        assert "'CO-12'" not in flagged

    @pytest.mark.asyncio
    async def test_a_real_hole_with_an_english_prefix_resolves(self) -> None:
        warnings = await verify_entities(
            "Show assays for CO-12 [NI43-1].", PROJECT, _CollarPool(["CO-12"]), None,
            [("query_spatial_collars", {"count": 1, "collars": [{"hole_id": "CO-12"}]})],
        )
        assert _critical(warnings) == [], warnings

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
    async def test_a_sample_id_beside_a_real_hole_is_not_checked_as_a_hole(self) -> None:
        """"sample MS240301" sits inside the 32-character window after "Hole",
        behind a clause boundary. It used to be asked of silver.collars and, absent
        there, reported."""
        collars = ("query_spatial_collars", {
            "count": 1, "collars": [{"hole_id": "PLS-22-08", "total_depth": 510.0}],
        })
        pool = _CollarPool(["PLS-22-08"])
        warnings = await verify_entities(
            "Hole PLS-22-08 (sample MS240301) reached a total depth of 510 m [DATA-1].",
            PROJECT, pool, None, [collars],
        )
        assert warnings == []
        assert [call[1] for call in pool.calls] == [["PLS-22-08"]]

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
