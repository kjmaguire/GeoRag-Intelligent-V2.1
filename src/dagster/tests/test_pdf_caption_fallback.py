"""Tests for _nearest_text_below_figure caption fallback (2026-05-23).

When docling's PictureItem.caption_text(doc) returns nothing, the
fallback should find the closest text item below the figure on the
same page. Uses lightweight stand-ins for the docling shapes so the
test doesn't require running docling end-to-end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest


@dataclass
class _BBox:
    l: float  # noqa: E741
    t: float
    r: float
    b: float
    coord_origin: str = "TOPLEFT"

    def to_top_left_origin(self, page_height: Optional[float]):
        # Already top-left in these tests.
        return self


@dataclass
class _Prov:
    page_no: int
    bbox: _BBox


@dataclass
class _Pic:
    prov: list[_Prov] = field(default_factory=list)


@dataclass
class _Txt:
    text: str
    prov: list[_Prov] = field(default_factory=list)


@dataclass
class _Page:
    size: object = field(default_factory=lambda: type("S", (), {"height": 1000.0})())


@dataclass
class _Doc:
    pictures: list[_Pic] = field(default_factory=list)
    texts: list[_Txt] = field(default_factory=list)
    pages: dict = field(default_factory=dict)


@pytest.fixture
def fallback():
    from georag_dagster.parsers.pdf_report import _nearest_text_below_figure
    return _nearest_text_below_figure


def _make_figure_at(page=1, l=100, t=200, r=400, b=400) -> _Pic:  # noqa: E741
    return _Pic(prov=[_Prov(page_no=page, bbox=_BBox(l, t, r, b))])


def _make_text(text, page=1, l=100, t=420, r=400, b=440) -> _Txt:  # noqa: E741
    return _Txt(text=text, prov=[_Prov(page_no=page, bbox=_BBox(l, t, r, b))])


def test_picks_nearest_text_below(fallback):
    fig = _make_figure_at(b=400)
    doc = _Doc(
        pictures=[fig],
        texts=[
            _make_text("Figure 1: Cross-section A-A'", t=410, b=425),
            _make_text("Some body text further down the page", t=600, b=620),
        ],
        pages={1: _Page()},
    )
    out = fallback(doc, fig, page_no=1)
    assert out == "Figure 1: Cross-section A-A'"


def test_skips_text_above_figure(fallback):
    fig = _make_figure_at(t=200, b=400)
    doc = _Doc(
        pictures=[fig],
        texts=[_make_text("Header text above the figure", t=50, b=70)],
        pages={1: _Page()},
    )
    assert fallback(doc, fig, page_no=1) == ""


def test_skips_text_too_far_below(fallback):
    fig = _make_figure_at(b=400)
    doc = _Doc(
        pictures=[fig],
        texts=[_make_text("Way down the page", t=900, b=920)],
        pages={1: _Page()},
    )
    assert fallback(doc, fig, page_no=1) == ""


def test_skips_text_on_other_pages(fallback):
    fig = _make_figure_at(page=2, b=400)
    doc = _Doc(
        pictures=[fig],
        texts=[_make_text("Caption on wrong page", page=1, t=410, b=425)],
        pages={1: _Page(), 2: _Page()},
    )
    assert fallback(doc, fig, page_no=2) == ""


def test_skips_page_number_noise(fallback):
    fig = _make_figure_at(b=400)
    doc = _Doc(
        pictures=[fig],
        texts=[
            _make_text("42", t=410, b=425),
            _make_text("Figure 1: Real caption further down", t=445, b=465),
        ],
        pages={1: _Page()},
    )
    assert fallback(doc, fig, page_no=1) == "Figure 1: Real caption further down"


def test_horizontal_alignment_tiebreaker(fallback):
    fig = _make_figure_at(l=100, t=200, r=400, b=400)  # center x ≈ 250
    doc = _Doc(
        pictures=[fig],
        texts=[
            _make_text("Sidebar text far from figure column", l=900, t=410, r=1100, b=425),
            _make_text("Aligned caption right under figure", l=100, t=420, r=400, b=435),
        ],
        pages={1: _Page()},
    )
    out = fallback(doc, fig, page_no=1)
    assert out == "Aligned caption right under figure"


def test_returns_empty_when_no_text(fallback):
    fig = _make_figure_at()
    doc = _Doc(pictures=[fig], texts=[], pages={1: _Page()})
    assert fallback(doc, fig, page_no=1) == ""


def test_returns_empty_when_picture_has_no_prov(fallback):
    doc = _Doc(pictures=[_Pic(prov=[])], texts=[], pages={1: _Page()})
    assert fallback(doc, _Pic(prov=[]), page_no=1) == ""
