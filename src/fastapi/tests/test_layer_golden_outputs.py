"""Realistic answers through the four §04i post-assembly guards.

Nothing in CI measured answer honesty. `ci.yml` excludes the golden,
hallucination, integration and live buckets; `eval-gate.yml` is nightly, stubs
the LLM and its own header says its verdict is "NOT trusted as a quality gate".
The §04i unit tests that DO gate merges exercise single-fact, single-number
sentences — "There are 5000 drill holes." — against one-key tool results, which
cannot expose anything that depends on a whole answer: proximity windows,
range tolerances, unit attribution, identifier collisions.

These fixtures are the cheap half of the fix the audit called for. No model, no
network, no database: realistic multi-number, multi-sentence geological answers
paired with realistic tool payloads, asserted against `verify_numbers` (L3),
`verify_entities` (L4), `verify_constraints` (L6) and `verify_completeness`.
They carry no pytest marker, so they run in the fast PR bucket.

Writing them found four defects on the first pass, three now fixed:

  * **Drill-hole IDs parsed as numbers.** "PLS-22-08" yielded -22.0 and -8.0
    on BOTH sides of Layer 3 — the answer text and the serialised evidence,
    where every collar row carries a hole_id. Those false small magnitudes fed
    the derivation tolerance, which accepts any value at the same order of
    magnitude as some grounded value, so a fabricated "7.44 g/t Au" was
    blessed by the ID of a hole that had nothing to do with it. Gold grades in
    g/t and widths in metres live in exactly the range hole-ID suffixes
    occupy, so Layer 3 was effectively off for the quantities it exists to
    protect.
  * **ppb compared against a ppm ceiling.** An ordinary "1020 ppb Au"
    (= 1.02 g/t) was reported as violating `grade_gold_max_ppm` (max 1000).
    ppb is the standard unit for low-grade gold.
  * **The same gap in reverse.** "2 % Au" is 20,000 ppm and passed.
  * **A unit outranking a commodity.** `\\bppm\\b` is a keyword of the GOLD
    constraint, and the governing constraint is the one whose keyword sits
    nearest the number — so "4200 ppm U3O8", a normal high-grade uranium
    value, was flagged as impossible gold.

The fourth is still open and pinned below under TestKnownGaps, because a fix
would remove real numbers from grounding, which is the more dangerous error.
"""

from __future__ import annotations

import pytest

from app.agent.hallucination.orchestrator_validators import (
    verify_completeness,
    verify_constraints,
    verify_entities,
    verify_numbers,
)


# --------------------------------------------------------------------------
# A stand-in for the asyncpg pool Layer 4's hole-ID check needs.
# --------------------------------------------------------------------------
# These fixtures used to pass pg_pool=None, which meant the drill-hole
# existence check never ran here at all — and after 2026-09-15 it cannot be
# None-and-silent any more: a check that cannot run now fails CLOSED and says
# the answer is unverified, rather than returning as though every hole
# resolved. Passing a pool that knows which holes exist is both closer to
# production (deps.pg_pool is typed asyncpg.Pool, never optional) and
# strictly more coverage than None ever gave.
class _FakePool:
    """Returns exactly the hole IDs it was told exist."""

    def __init__(self, known: set[str]) -> None:
        self._known = known

    def acquire(self):  # noqa: ANN202 — mimics asyncpg's async context manager
        known = self._known

        class _Conn:
            async def fetch(self, _sql: str, hole_ids: list[str], *_args: object):
                return [{"hole_id": h} for h in hole_ids if h in known]

        class _Acquire:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return _Acquire()


#: The holes the COLLARS fixture actually contains.
REAL_HOLES = {"PLS-22-08", "PLS-22-09", "PLS-22-10"}


# --------------------------------------------------------------------------
# Evidence — the shapes the tools really return, not one-key stand-ins.
# --------------------------------------------------------------------------

COLLARS = ("query_spatial_collars", {
    "count": 3,
    "data_source": "postgis:silver.collars",
    "collars": [
        {"hole_id": "PLS-22-08", "total_depth": 510.0, "azimuth": 45.0,
         "dip": -60.0, "easting": 512345.7, "northing": 6543210.1},
        {"hole_id": "PLS-22-09", "total_depth": 498.0, "azimuth": 45.0,
         "dip": -60.0, "easting": 512401.2, "northing": 6543188.9},
        {"hole_id": "PLS-22-10", "total_depth": 372.5, "azimuth": 90.0,
         "dip": -55.0, "easting": 512490.0, "northing": 6543150.4},
    ],
})

ASSAYS = ("query_assay_data", {
    "count": 2,
    "element": "Au",
    "data_source": "postgis:silver.samples",
    "min_value": 0.12, "max_value": 2.31,
    "mean_value": 1.02, "median_value": 0.87,
    "samples": [
        {"hole_id": "PLS-22-08", "from_m": 145.2, "to_m": 148.0, "value": 2.31},
        {"hole_id": "PLS-22-09", "from_m": 151.0, "to_m": 154.0, "value": 0.87},
    ],
})

DOCS = ("search_documents", {
    "count": 1,
    "data_source": "qdrant:georag_chunks",
    "chunks": [{
        "text": "The Rowan zone was drilled in 2022 over 12.5 m at 1.85 g/t Au.",
        "relevance_score": 0.91,
        "document_title": "Rowan Technical Report",
        "page": 88,
    }],
})


def _numbers(text: str, tools=None) -> list[str]:
    return verify_numbers(text, tools if tools is not None else [COLLARS, ASSAYS])


# --------------------------------------------------------------------------
# The answers a geologist would actually get back.
# --------------------------------------------------------------------------

GROUNDED = (
    "Three holes were drilled at Rowan [DATA-1]. PLS-22-08 reached 510 m and "
    "returned 2.31 g/t Au over the 145.2 to 148.0 m interval [DATA-2]. "
    "PLS-22-09 reached 498 m with 0.87 g/t Au [DATA-2]. "
    "The mean grade across the programme is 1.02 g/t Au [DATA-2]."
)

ONE_FABRICATED_GRADE = (
    "PLS-22-08 reached 510 m [DATA-1] and returned 2.31 g/t Au [DATA-2]. "
    "PLS-22-09 reached 498 m [DATA-1] and returned 7.44 g/t Au [DATA-2]."
)

NEGATIVE_FINDING = (
    "PLS-22-08 returned 2.31 g/t Au over the 145.2 to 148.0 m interval "
    "[DATA-2]. Core recovery data is not available for the upper 40 m "
    "[DATA-1]."
)

BARE_ASSERTION = (
    "PLS-22-08 reached 510 m [DATA-1]. The deposit is clearly economic and "
    "warrants immediate development."
)


class TestAGroundedAnswerIsLeftAlone:
    """False positives are the expensive failure. A guard that fires on
    correct answers gets ignored, and then it is not a guard."""

    def test_layer_3_reports_nothing(self) -> None:
        assert _numbers(GROUNDED) == []

    def test_layer_6_reports_nothing(self) -> None:
        assert verify_constraints(GROUNDED) == []

    def test_completeness_passes(self) -> None:
        assert verify_completeness(GROUNDED) == []

    @pytest.mark.asyncio
    async def test_layer_4_reports_nothing(self) -> None:
        """Now runs the hole-ID check for real, against a pool that knows the
        fixture's holes — where it previously passed pg_pool=None and skipped
        it entirely."""
        warnings = await verify_entities(
            GROUNDED, "proj", _FakePool(REAL_HOLES), None, [COLLARS, ASSAYS],
        )

        assert warnings == []

    def test_a_negative_finding_is_not_a_defect(self) -> None:
        """"Core recovery data is not available for the upper 40 m" is the
        behaviour the citation contract asks for, not a refusal and not a
        fabrication. 40 is ungrounded in the literal sense — no tool returned
        it — and must survive the derivation tolerance."""
        assert _numbers(NEGATIVE_FINDING) == []
        assert verify_constraints(NEGATIVE_FINDING) == []


class TestLayer3CatchesAFabricatedGrade:
    """The case that was silently passing until 2026-08-21."""

    def test_the_invented_grade_is_flagged(self) -> None:
        warnings = _numbers(ONE_FABRICATED_GRADE)

        assert len(warnings) == 1
        assert "7.44" in warnings[0]

    def test_and_only_the_invented_one(self) -> None:
        """510, 498 and 2.31 are all in evidence. A guard that flags the
        answer wholesale tells the reader nothing about where to look."""
        warnings = _numbers(ONE_FABRICATED_GRADE)
        joined = " ".join(warnings)

        for grounded in ("510", "498", "2.31"):
            assert grounded not in joined

    def test_hole_ids_do_not_become_numbers(self) -> None:
        """The mechanism. Without the identifier strip, "PLS-22-08" put
        -22.0 and -8.0 in the number list, and the same IDs in the collar
        payload put them in the grounded set."""
        from app.agent.hallucination.orchestrator_validators import (
            _extract_numbers_from_text,
        )

        assert _extract_numbers_from_text(
            "Hole DDH-22-041 reached 510 m."
        ) == [510.0]

    def test_a_number_far_outside_the_evidence_scale_is_flagged(self) -> None:
        assert any("5000" in w for w in _numbers(
            "There are 5000 drill holes at Rowan [DATA-1]."
        ))


class TestLayer6UnitHandling:
    """Bounds are expressed in one unit; answers are written in whichever
    unit the source used."""

    def test_ppb_is_not_read_as_ppm(self) -> None:
        """1020 ppb is 1.02 g/t — an ordinary low-grade gold assay. It was
        compared against the 1000 **ppm** ceiling and called impossible."""
        assert verify_constraints(
            "The composite assayed 1.02 g/t Au [DATA-2], equivalent to "
            "1020 ppb."
        ) == []

    def test_percent_gold_is_converted_up(self) -> None:
        """The same gap running the other way: 2 % Au is 20,000 ppm, twenty
        times the ceiling, and it used to pass as "2"."""
        warnings = verify_constraints("The zone averages 2 % Au [DATA-1].")

        assert len(warnings) == 1
        assert "grade_gold_max_ppm" in warnings[0]

    def test_g_per_tonne_needs_no_conversion(self) -> None:
        assert verify_constraints("PLS-22-08 returned 2.31 g/t Au [DATA-2].") == []

    def test_a_bonanza_grade_below_the_ceiling_still_passes(self) -> None:
        """145 g/t Au is extraordinary but real. The 1000 ppm ceiling is an
        SME-set limit in layer6_constraints.json and this test exists so a
        unit change never quietly moves it."""
        assert verify_constraints(
            "PLS-22-08 returned 145 g/t Au over 12.5 m [DATA-2]."
        ) == []

    def test_a_uranium_grade_in_ppm_is_not_a_gold_claim(self) -> None:
        """`\\bppm\\b` is a keyword of the gold constraint, and the governing
        constraint is the one whose keyword sits nearest the number — the
        unit is adjacent, the commodity is one token further. So a normal
        high-grade uranium value was reported as impossible gold."""
        assert verify_constraints(
            "Uranium grades reach 4200 ppm U3O8 [DATA-1]."
        ) == []

    def test_uranium_is_still_checked_against_its_own_ceiling(self) -> None:
        """Skipping the gold constraint must not mean skipping every one."""
        warnings = verify_constraints("The zone averages 61 % U3O8 [DATA-1].")

        assert len(warnings) == 1
        assert "grade_uranium_max_pct" in warnings[0]

    def test_another_commodity_is_left_alone(self) -> None:
        assert verify_constraints("Copper grades average 0.8 % Cu [DATA-1].") == []

    def test_an_impossible_depth_is_still_caught(self) -> None:
        warnings = verify_constraints(
            "PLS-22-08 reached a total depth of 14000 m [DATA-1]."
        )

        assert len(warnings) == 1
        assert "depth_max_m" in warnings[0]


class TestCompleteness:
    def test_a_bare_assertion_fails(self) -> None:
        """"The deposit is clearly economic" carries no marker and follows a
        sentence that does — the exact shape CLAUDE.md hard rule 4 forbids."""
        assert verify_completeness(BARE_ASSERTION) != []

    def test_a_fully_cited_answer_passes(self) -> None:
        assert verify_completeness(GROUNDED) == []


class TestThingsThatLookLikeClaimsAndAreNot:
    def test_citation_marker_numbers_are_not_measurements(self) -> None:
        assert _numbers(
            "PLS-22-08 reached 510 m [NI43-12] and 2.31 g/t Au [NI43-7]."
        ) == []

    def test_a_year_is_not_a_measurement(self) -> None:
        assert _numbers(
            "The Rowan zone was drilled in 2022 over 12.5 m at 1.85 g/t Au "
            "[NI43-1].",
            [DOCS],
        ) == []

    def test_coordinates_do_not_trip_the_depth_ceiling(self) -> None:
        """512345.7 is an easting, and the depth constraint's negative
        keywords exist for exactly this."""
        assert verify_constraints(
            "The collar sits at easting 512345.7 and northing 6543210.1 "
            "[DATA-1]."
        ) == []


class TestKnownGaps:
    """Pinned, not fixed. A gap nobody has written down is a gap that gets
    rediscovered as a production incident."""

    def test_bare_numeric_hole_ids_still_parse_as_numbers(self) -> None:
        """36-1085 and 36-1042 are the Cameco Shirley Basin convention, and
        the identifier strip deliberately does not match them: the same shape
        is a year range ("2021-2022"), a page range and an interval written
        with a hyphen ("145.2-148.0 m"). Stripping those would remove REAL
        numbers from grounding, which is the more dangerous error — a number
        missing from the grounded set makes a true statement look fabricated.

        Fixing this properly needs the context gate `viz_builder` already
        applies to the same pattern.
        """
        from app.agent.hallucination.orchestrator_validators import (
            _extract_numbers_from_text,
        )

        assert _extract_numbers_from_text("Hole 36-1085 reached 210 m.") == [
            36.0, -1085.0, 210.0,
        ]

    def test_a_hyphenated_interval_yields_a_spurious_negative(self) -> None:
        """"145.2-148.0 m" gives -148.0. Same root cause, same reason it is
        left alone. The "145.2 to 148.0 m" form the model is prompted to use
        is unaffected."""
        from app.agent.hallucination.orchestrator_validators import (
            _extract_numbers_from_text,
        )

        assert -148.0 in _extract_numbers_from_text(
            "The interval 145.2-148.0 m assayed 2.31 g/t."
        )

    @pytest.mark.asyncio
    async def test_a_fabricated_hole_id_is_now_caught_here(self) -> None:
        """This used to be `test_a_fabricated_hole_id_passes_here`, asserting
        the gap rather than the guard: every fixture passed pg_pool=None, so
        Layer 4's drill-hole existence check never ran and DDH-9999 sailed
        through with an empty warning list.

        A pool costs one small class (`_FakePool`), so the gap was avoidable
        rather than inherent. It closed on 2026-09-15 alongside the
        fail-closed fix in verify_entities.
        """
        warnings = await verify_entities(
            "PLS-22-08 reached 510 m [DATA-1]. DDH-9999 reached 372.5 m "
            "[DATA-1].",
            "proj", _FakePool(REAL_HOLES), None, [COLLARS],
        )

        assert any("DDH-9999" in w for w in warnings), (
            "a hole absent from both silver.collars and the retrieved "
            "evidence must be reported"
        )
        assert not any("PLS-22-08" in w for w in warnings), (
            "a real hole must not be reported"
        )

    @pytest.mark.asyncio
    async def test_a_pool_that_is_down_does_not_read_as_clean(self) -> None:
        """The other half of the same fix. A check that could not run must
        not return an empty warning list — that is indistinguishable from
        'checked and fine', and it is what shipped before."""
        class _DeadPool:
            def acquire(self):  # noqa: ANN202
                raise ConnectionError("pool exhausted")

        warnings = await verify_entities(
            "PLS-22-08 reached 510 m [DATA-1]. DDH-9999 reached 372.5 m "
            "[DATA-1].",
            "proj", _DeadPool(), None, [COLLARS],
        )

        assert any("UNVERIFIED" in w for w in warnings)


class TestHoleIdSubstringFalsePositive:
    """The numeric tail of a real hole ID is not a second hole.

    `\\b` sits between the "-" and the "22" of "PLS-22-08", so the bare-numeric
    pattern pulls "22-08" out of it. That phantom is absent from
    silver.collars, and the warning it produced carries the one prefix the
    severity classifier treats as critical on its own — so an ordinary correct
    answer naming a real hole had its confidence floored and should_retry set.

    It survived because every fixture in this file passed pg_pool=None, which
    meant the lookup never ran and the file asserted the silence.
    """

    @pytest.mark.asyncio
    async def test_a_real_hole_does_not_produce_a_phantom(self) -> None:
        warnings = await verify_entities(
            "Three holes were drilled. PLS-22-08 reached 510 m [DATA-1].",
            "proj", _FakePool(REAL_HOLES), None, [COLLARS],
        )

        assert not any("22-08'" in w for w in warnings), (
            f"the numeric tail of PLS-22-08 was reported as its own hole: "
            f"{warnings}"
        )
        assert warnings == []

    @pytest.mark.asyncio
    async def test_a_genuinely_bare_numeric_hole_is_still_checked(self) -> None:
        """The Cameco Shirley Basin shape the bare-numeric pattern exists for.

        Nothing alphanumeric contains it, so the filter must leave it alone.
        """
        warnings = await verify_entities(
            "Hole 36-1085 reached 210 m [DATA-1].",
            "proj", _FakePool(REAL_HOLES), None, [COLLARS],
        )

        assert any("36-1085" in w for w in warnings), (
            "a fabricated bare-numeric hole must still be caught"
        )
