"""The three hole-ID shapes this corpus actually contains.

Two consumers depend on these patterns and used to disagree about them.
viz_builder used them to route a query to a collar lookup and recognised all
three shapes. Layer 4's entity resolution carried its own narrower copy --
letters plus TWO dash-separated numeric groups, case-sensitive -- so
`36-9999` and `DDH-1234` were never checked against silver.collars at all.

That gap had no backstop: the fabricated-hole-ID warning is the single
Layer 4 warning the severity classifier grades critical on its own, so a
model inventing "hole 36-9999 intersected 4.2 m at 8.1 g/t Au" produced no
warning, no retry, and shipped at full confidence.
"""

from __future__ import annotations

import pytest

from app.agent.hole_id_patterns import (
    HOLE_CONTEXT_RE,
    HOLE_ID_RE,
    NUMERIC_HOLE_ID_RE,
)

LETTERED = [
    "PLS-22-08",      # Patterson Lake South
    "GH08-212",       # Wyoming historical, embedded year
    "SRE09-12",       # WSGS SRE
    "IC-11",          # two-letter prefix, single group
    "XLS-24-01",
    "DH-2547",
    "DDH-1234",       # invisible to the old Layer 4 pattern
    "MSD-2024-0001",
    "DDH-123456-7",   # six-digit group; the old pattern allowed it
]

NUMERIC = [
    "36-1085",        # Cameco Shirley Basin
    "36-1042",
    "0070-4850",      # Gas Hills
    "3774-36-1458",   # three numeric groups
]

# Every one of these appears on nearly every page of an NI 43-101. A false
# positive here is not harmless: Layer 4 reads an unmatched ID as fabricated
# and floors the answer's confidence behind a fabrication banner.
NOT_HOLE_IDS = [
    "Figure A-1",
    "Table B-2",
    "Appendix C-3",
    "see section 14-1 for details",
    "the interval 20-30 m",
    "pages 11-14",
]


@pytest.mark.parametrize("hole_id", LETTERED)
def test_lettered_ids_match(hole_id: str) -> None:
    assert HOLE_ID_RE.fullmatch(hole_id), hole_id


@pytest.mark.parametrize("hole_id", NUMERIC)
def test_numeric_ids_match(hole_id: str) -> None:
    assert NUMERIC_HOLE_ID_RE.fullmatch(hole_id), hole_id


@pytest.mark.parametrize("text", NOT_HOLE_IDS)
def test_document_furniture_is_not_a_hole_id(text: str) -> None:
    assert not HOLE_ID_RE.search(text), text


def test_numeric_ids_need_a_hole_context_word() -> None:
    """Bare digit pairs are only IDs when the text is talking about holes."""
    assert not HOLE_CONTEXT_RE.search("the interval 20-30 m assayed 1.2 g/t")
    assert HOLE_CONTEXT_RE.search("hole 36-1085 was collared in 1978")
    assert HOLE_CONTEXT_RE.search("this drillhole, 36-1085, is the deepest")


def test_case_insensitive() -> None:
    assert HOLE_ID_RE.fullmatch("pls-22-08")
    assert HOLE_ID_RE.fullmatch("Pls-22-08")
