"""Tests for the vector-figure path in app.agent.figure_extractor.

Covers _cluster_boxes (pure), detect_vector_figure_regions (with a fake
pypdfium2 so the clustering/filtering logic is exercised in isolation), and
extract_vector_figures_from_pdf chaining. A final section drives the REAL
pypdfium2 library end-to-end against a hand-crafted vector PDF (no reportlab
dependency) so the actual detect→render→crop integration is covered, not just
the clustering math. These add the coverage the raster-only test file lacked
and guard the heuristic thresholds against silent regressions.
"""
from __future__ import annotations

import sys
import types

import pytest

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


# --- real pypdfium2 integration (dependency-free hand-crafted vector PDF) ------
#
# The tests above drive the vector path through a FAKE pypdfium2, so the
# clustering/filtering math is covered without a real PDF. That leaves the
# actual integration point untested: does REAL pypdfium2 surface a real PDF's
# stroked paths as FPDF_PAGEOBJ_PATH objects with usable bounds, and does the
# render+crop then produce a figure? That gap is exactly where "figure path
# built but nothing flows" hides (it is why vector figures silently stopped
# after the PyMuPDF removal). These drive the real library end-to-end on a
# hand-crafted PDF — no reportlab dependency — that strokes a clustered grid of
# lines, standing in for a geological cross-section drawn as vector graphics.


def _vector_pdf_bytes() -> bytes:
    """One-page letter PDF stroking a dense, clustered grid of line paths in a
    200x250 pt region at (200, 250).

    >= 6 PATH objects clear ``min_objects``; the diagonal strokes keep the
    rendered crop above ``MIN_IMAGE_BYTES`` (an axis-aligned-only grid
    compresses under 5 KB and would be filtered).
    """
    ox, oy, w, h = 200, 250, 200, 250
    ops = ["1 w"]
    for i in range(6):  # horizontal rules
        y = oy + i * (h // 5)
        ops.append(f"{ox} {y} m {ox + w} {y} l S")
    for j in range(6):  # vertical rules
        x = ox + j * (w // 5)
        ops.append(f"{x} {oy} m {x} {oy + h} l S")
    for k in range(9):  # diagonals — defeat PNG run-length compression
        xa = ox + k * (w // 9)
        ops.append(f"{ox} {oy} m {xa} {oy + h} l S")
        ops.append(f"{ox + w} {oy} m {xa} {oy + h} l S")
    stream = ("\n".join(ops)).encode("latin-1")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1) + b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1, xref_pos,
    )
    return bytes(out)


def _write_vector_pdf(tmp_path) -> str:
    pdf = tmp_path / "handcrafted_vector.pdf"
    pdf.write_bytes(_vector_pdf_bytes())
    return str(pdf)


def test_detect_vector_regions_on_real_pdf(tmp_path):
    """Real pypdfium2 parses the stroked paths and clusters them into one region
    covering the drawn 200x250 pt area."""
    pytest.importorskip("pypdfium2")
    regions = detect_vector_figure_regions(_write_vector_pdf(tmp_path))

    assert len(regions) == 1
    region = regions[0]
    assert region["page"] == 1
    left, bottom, right, top = region["bbox"]
    # Region tracks the drawn box at (200,250)-(400,500), within a stroke width.
    assert 195 <= left <= 205 and 245 <= bottom <= 255
    assert 395 <= right <= 405 and 495 <= top <= 505


def test_extract_vector_figures_on_real_pdf(tmp_path):
    """Full path: real pypdfium2 detect → render → crop yields one figure whose
    PNG clears MIN_IMAGE_BYTES and carries the source bbox."""
    pytest.importorskip("pypdfium2")
    figs = extract_vector_figures_from_pdf(_write_vector_pdf(tmp_path))

    assert len(figs) == 1
    fig = figs[0]
    assert fig["page"] == 1
    assert fig["format"] == "png"
    assert fig["width"] >= fe.MIN_IMAGE_DIMENSION or fig["height"] >= fe.MIN_IMAGE_DIMENSION
    assert len(fig["image_bytes"]) >= fe.MIN_IMAGE_BYTES
    assert len(fig["sha256"]) == 64
    assert len(fig["bbox"]) == 4


def test_extract_vector_figures_honours_exclude_pages_on_real_pdf(tmp_path):
    """Excluding the only page short-circuits detection end-to-end."""
    pytest.importorskip("pypdfium2")
    out = extract_vector_figures_from_pdf(
        _write_vector_pdf(tmp_path), exclude_pages={1},
    )
    assert out == []
