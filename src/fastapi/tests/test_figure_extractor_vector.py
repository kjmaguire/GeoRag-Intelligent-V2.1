"""Tests for the vector-figure path in app.agent.figure_extractor.

Covers _cluster_boxes (pure), detect_vector_figure_regions (with a fake
pypdfium2 so the clustering/filtering logic is exercised without a real
vector PDF — reportlab is not a dependency), and extract_vector_figures_from_pdf
chaining. These add the coverage the raster-only test file lacked and guard the
heuristic thresholds against silent regressions.
"""
from __future__ import annotations

import sys
import types

from app.agent import figure_extractor as fe
from app.agent.figure_extractor import (
    _cluster_boxes,
    detect_vector_figure_regions,
    extract_vector_figures_from_pdf,
)


# --- _cluster_boxes (pure) ---------------------------------------------------

def test_cluster_boxes_single():
    assert _cluster_boxes([(0.0, 0.0, 10.0, 10.0)], gap=1.0) == [((0.0, 0.0, 10.0, 10.0), 1)]


def test_cluster_boxes_merges_overlapping():
    out = _cluster_boxes([(0, 0, 10, 10), (5, 5, 15, 15)], gap=0.0)
    assert out == [((0, 0, 15, 15), 2)]


def test_cluster_boxes_gap_bridges_near_boxes():
    # 4pt apart: separate at gap=1, one cluster at gap=5.
    boxes = [(0, 0, 10, 10), (14, 0, 24, 10)]
    assert len(_cluster_boxes(boxes, gap=1.0)) == 2
    assert _cluster_boxes(boxes, gap=5.0) == [((0, 0, 24, 10), 2)]


def test_cluster_boxes_keeps_distant_separate():
    boxes = [(0, 0, 10, 10), (500, 500, 510, 510)]
    assert len(_cluster_boxes(boxes, gap=2.0)) == 2


def test_cluster_boxes_transitive_chain_counts_members():
    # A touches B touches C — all one cluster (count 3) even though A and C
    # don't overlap directly.
    boxes = [(0, 0, 10, 10), (9, 0, 19, 10), (18, 0, 28, 10)]
    assert _cluster_boxes(boxes, gap=0.0) == [((0, 0, 28, 10), 3)]


# --- fake pypdfium2 harness for detect_vector_figure_regions -----------------

class _FakeObj:
    def __init__(self, bounds):
        self._bounds = bounds

    def get_bounds(self):
        return self._bounds


class _FakePage:
    def __init__(self, size, objs):
        self._size = size
        self._objs = objs

    def get_size(self):
        return self._size

    def get_objects(self, filter=None):  # noqa: A002 — mirrors pypdfium2 kwarg
        return list(self._objs)


class _FakePdf:
    def __init__(self, pages):
        self._pages = pages

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, i):
        return self._pages[i]

    def close(self):
        return None


def _install_fake_pdfium(monkeypatch, pages):
    fake = types.ModuleType("pypdfium2")
    fake.PdfDocument = lambda _path: _FakePdf(pages)
    raw = types.ModuleType("pypdfium2.raw")
    raw.FPDF_PAGEOBJ_PATH = 4
    raw.FPDF_PAGEOBJ_SHADING = 5
    fake.raw = raw
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)
    monkeypatch.setitem(sys.modules, "pypdfium2.raw", raw)


_LETTER = (612.0, 792.0)  # US-Letter in points


def _dense_region_objs(n: int = 6) -> list[_FakeObj]:
    """A 3x2 grid of overlapping vector objects covering left=100..400,
    bottom=400..700 (300x300pt, ~18.5% of a Letter page). Returns the first
    ``n`` (all overlap, so any prefix stays a single cluster)."""
    grid = [
        (100, 400, 220, 560), (190, 400, 310, 560), (280, 400, 400, 560),
        (100, 540, 220, 700), (190, 540, 310, 700), (280, 540, 400, 700),
    ]
    return [_FakeObj(b) for b in grid[:n]]


def test_detect_finds_clustered_figure(monkeypatch):
    # 6 dense vector paths spanning the region → one figure region.
    _install_fake_pdfium(monkeypatch, [_FakePage(_LETTER, _dense_region_objs(6))])
    regions = detect_vector_figure_regions("/x.pdf")
    assert len(regions) == 1
    r = regions[0]
    assert r["page"] == 1                       # 1-based
    assert r["bbox"] == [100, 400, 400, 700]


def test_detect_skips_sparse_cluster(monkeypatch):
    # 5 objects (< min_objects=6) — a frame/rule-lines-shaped cluster, not a
    # figure — is rejected even though its bbox passes the size/area filters.
    _install_fake_pdfium(monkeypatch, [_FakePage(_LETTER, _dense_region_objs(5))])
    assert detect_vector_figure_regions("/x.pdf") == []
    # Lowering the floor admits it → proves the count filter is what rejected it.
    assert len(detect_vector_figure_regions("/x.pdf", min_objects=5)) == 1


def test_detect_skips_small_cluster(monkeypatch):
    # 40x40pt cluster < min_dimension_pts (72) → dropped (6 tiny stacked objs).
    objs = [_FakeObj((100 + i, 100, 140, 140 + i)) for i in range(6)]
    _install_fake_pdfium(monkeypatch, [_FakePage(_LETTER, objs)])
    assert detect_vector_figure_regions("/x.pdf") == []


def test_detect_skips_full_page_frame(monkeypatch):
    # A near-full-page border (frac > max_area_frac) is page decoration, not a
    # figure — rejected on area even with enough objects.
    objs = [_FakeObj((5, 5 + i, 607, 787)) for i in range(6)]
    _install_fake_pdfium(monkeypatch, [_FakePage(_LETTER, objs)])
    assert detect_vector_figure_regions("/x.pdf") == []


def test_detect_skips_frame_corner_hairlines(monkeypatch):
    # Two perpendicular thin strokes (a frame corner) cluster to a big empty
    # bbox — the classic false positive. min_objects rejects it.
    objs = [_FakeObj((100, 500, 400, 500.5)), _FakeObj((100, 500, 100.5, 700))]
    _install_fake_pdfium(monkeypatch, [_FakePage(_LETTER, objs)])
    assert detect_vector_figure_regions("/x.pdf") == []


def test_detect_honours_exclude_pages(monkeypatch):
    _install_fake_pdfium(monkeypatch, [_FakePage(_LETTER, _dense_region_objs(6))])
    # Page 1 excluded (e.g. classified as a ruled-table page) → no regions.
    assert detect_vector_figure_regions("/x.pdf", exclude_pages={1}) == []


def test_detect_open_failure_returns_empty(monkeypatch):
    fake = types.ModuleType("pypdfium2")

    def _boom(_path):
        raise RuntimeError("cannot open")

    fake.PdfDocument = _boom
    raw = types.ModuleType("pypdfium2.raw")
    raw.FPDF_PAGEOBJ_PATH = 4
    raw.FPDF_PAGEOBJ_SHADING = 5
    fake.raw = raw
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)
    assert detect_vector_figure_regions("/x.pdf") == []


# --- extract_vector_figures_from_pdf chaining --------------------------------

def test_extract_vector_no_regions_returns_empty(monkeypatch):
    monkeypatch.setattr(fe, "detect_vector_figure_regions", lambda *a, **k: [])
    assert extract_vector_figures_from_pdf("/x.pdf") == []


def test_extract_vector_forwards_regions_to_layout(monkeypatch):
    regions = [{"page": 1, "bbox": [100, 400, 400, 700]}]
    captured = {}

    def _fake_layout(pdf_path, figure_regions, *, render_scale=2.0):
        captured["path"] = pdf_path
        captured["regions"] = figure_regions
        captured["scale"] = render_scale
        return [{"page": 1, "bbox": figure_regions[0]["bbox"], "image_bytes": b"png"}]

    monkeypatch.setattr(fe, "detect_vector_figure_regions", lambda *a, **k: regions)
    monkeypatch.setattr(fe, "extract_figures_from_layout", _fake_layout)

    out = extract_vector_figures_from_pdf("/x.pdf", render_scale=3.0, exclude_pages={2})
    assert out and out[0]["bbox"] == [100, 400, 400, 700]
    assert captured["path"] == "/x.pdf"
    assert captured["regions"] == regions
    assert captured["scale"] == 3.0
