"""A follow-up question must not be rewritten against a citation marker.

`extract_entity_mentions` is the fallback that feeds pronoun resolution, and
it is not really a fallback: the Laravel bridge never writes `entity_mentions`
into `chat_messages.metadata`, so it fires on every turn of every
conversation -- including the assistant's own previous answers, which are
where citation markers live.

Its old pattern was `[A-Z]{0,4}-?\\d{1,4}(?:[-\\s]\\d{1,4}){0,3}`. The `{0,4}`
on the letter prefix greedily ate the `NI43` out of `[NI43-2]`, and the
marker's own dash satisfied the "must contain a dash" filter. So
"Average grade was 1.85 g/t Au over 12.5 m [NI43-2]." produced the hole
mention `NI43-2`, and the user's next question -- "what is its depth?" -- was
rewritten into "what is NI43-2's depth?" before the intent classifier, the
retrieval profile and every downstream hole-ID extractor ever saw it.

The retrieval was then built on a citation index, and the only thing shown to
the user was a small "Interpreted as:" chip.
"""

from __future__ import annotations

import pytest

from app.agent.multi_turn_resolver import extract_entity_mentions


def _holes(text: str) -> list[str]:
    return [
        m.surface_form
        for m in extract_entity_mentions(text, turn_index=0)
        if m.entity_type == "hole"
    ]


class TestCitationMarkersAreNotHoleIds:
    @pytest.mark.parametrize(
        "assistant_turn",
        [
            "Average grade was 1.85 g/t Au over 12.5 m [NI43-2].",
            "Mineralisation occurs between 120 and 145 m depth [NI43-1].",
            "The 2024 drill program totalled 1,250 m of core [DATA-1].",
            "Two sources agree on the tonnage [PUB-7] [NI43-3].",
        ],
    )
    def test_an_assistant_turn_yields_no_hole_from_its_markers(
        self, assistant_turn: str
    ) -> None:
        assert _holes(assistant_turn) == []


class TestRealHoleIdsStillResolve:
    def test_a_lettered_hole_is_extracted(self) -> None:
        assert _holes("Hole PLS-22-08 reached a total depth of 510 m [DATA-3].") == [
            "PLS-22-08"
        ]

    def test_a_numeric_hole_needs_a_hole_context_word(self) -> None:
        assert _holes("The hole 36-1085 was collared in 1978.") == ["36-1085"]

    def test_a_lettered_hole_does_not_also_yield_its_own_tail(self) -> None:
        """PLS-22-08 contains 22-08; taking both gives the resolver two
        candidates for one hole, and it picks by recency."""
        assert _holes("Hole PLS-22-08 was re-logged.") == ["PLS-22-08"]


class TestOrdinaryProseIsNotAHoleId:
    @pytest.mark.parametrize(
        "text",
        [
            "See pages 11-14 of the report for the 20-30 m interval.",
            "Figure A-1 shows the section.",
            "The 2024 drill program totalled 1,250 m of core.",
            "Recovery averaged 94% over 1-2 m runs.",
        ],
    )
    def test_no_hole_is_invented(self, text: str) -> None:
        assert _holes(text) == []
