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


# ---------------------------------------------------------------------------
# Audit 2026-10-04 item 24: separator positions between digit groups matter
# ---------------------------------------------------------------------------


def test_hole_id_key_distinguishes_digit_group_boundaries() -> None:
    from app.agent.hole_id_patterns import canonical_hole_id, hole_id_key

    # The legacy separator-free form merges two different holes ...
    assert canonical_hole_id("PLS-2-28") == canonical_hole_id("PLS-22-8")
    # ... the key does not.
    assert hole_id_key("PLS-2-28") != hole_id_key("PLS-22-8")
    assert hole_id_key("PLS-2-28") == "PLS2-28"
    assert hole_id_key("PLS-22-8") == "PLS22-8"


def test_hole_id_key_still_unifies_harmless_spelling_variants() -> None:
    from app.agent.hole_id_patterns import hole_id_key

    for variants in (
        ("PLS-22-08", "pls 22 08", "PLS22-08", "PLS_22.08", "PLS 22-08"),
        ("BH-12", "BH12", "bh 12", "Bh_12"),
        ("36-1085", "36 1085", "36.1085", "36_1085"),
        ("GH08-212", "gh08-212", "GH08 212"),
    ):
        assert len({hole_id_key(v) for v in variants}) == 1, variants


def test_hole_id_key_keeps_leading_zeros_like_the_canonical_form() -> None:
    from app.agent.hole_id_patterns import hole_id_key

    assert hole_id_key("BH-1") != hole_id_key("BH-01")


def test_hole_id_key_is_total() -> None:
    from app.agent.hole_id_patterns import hole_id_key

    assert hole_id_key("") == ""
    assert hole_id_key(None) == ""  # type: ignore[arg-type]
    assert hole_id_key("---") == ""
