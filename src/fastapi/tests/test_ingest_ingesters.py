"""Unit tests for Phase B/C/D/E.1 ingesters.

Doc-phase 183.

Covers:
  - las_ingester: PLSS coord derivation, date parsing, hole offset
  - cameco_log_ingester: regex parsing of binary headers + ft→m
  - pdf_ingester: chunking helpers
  - kg_sync: deposit-type mapping
  - passage_embedder: point ID generation
  - tiff_ocr_ingester: garbage filter

These tests are pure-Python (no DB / no model / no Qdrant) so they
run fast in the substrate verifier.
"""
from __future__ import annotations

import io
import zipfile

import pytest


# Phase H — module-level guard for las_ingester tests. The pyproject pin
# (`lasio>=0.31`) was added doc-phase 179 but the current FastAPI image
# was built before the pin landed, so the module is missing on import.
# Skip rather than fail; tests will auto-green once the operator rebuilds
# the image (see docs/phase_h_python_deps_audit.md step 1).
try:
    import lasio as _lasio_probe  # noqa: F401
    _LASIO_AVAILABLE = True
except ImportError:
    _LASIO_AVAILABLE = False

_requires_lasio = pytest.mark.skipif(
    not _LASIO_AVAILABLE,
    reason="lasio not installed in this image — rebuild fastapi after pin lands",
)


# ────────────────────────── las_ingester ──────────────────────────

@_requires_lasio
def test_las_parse_plss_loc_standard_format():
    from app.services.ingest.las_ingester import _parse_plss_loc
    # Standard LAS LOC field: "section township range"
    assert _parse_plss_loc("36    28    79") == (36, 28, 79)
    assert _parse_plss_loc("1 2 3") == (1, 2, 3)


@_requires_lasio
def test_las_parse_plss_loc_handles_missing_or_malformed():
    from app.services.ingest.las_ingester import _parse_plss_loc
    assert _parse_plss_loc("") is None
    assert _parse_plss_loc(None) is None
    assert _parse_plss_loc("only_one") is None  # only 1 number


@_requires_lasio
def test_las_hole_offset_meters_deterministic_and_bounded():
    from app.services.ingest.las_ingester import _hole_offset_meters
    de1, dn1 = _hole_offset_meters("36-1042")
    de2, dn2 = _hole_offset_meters("36-1042")
    # Deterministic: same hole → same offset
    assert (de1, dn1) == (de2, dn2)
    # Bounded: ±800m within section
    assert -800 < de1 < 800
    assert -800 < dn1 < 800


@_requires_lasio
def test_las_hole_offset_differs_per_hole():
    from app.services.ingest.las_ingester import _hole_offset_meters
    a = _hole_offset_meters("36-1042")
    b = _hole_offset_meters("36-1043")
    assert a != b  # different hole_ids → different offsets


@_requires_lasio
def test_las_derive_coordinates_uses_section_reference():
    from app.services.ingest.las_ingester import _derive_coordinates
    e, n = _derive_coordinates("028N079W36", "36-1042")
    # Shirley Basin reference: UTM Z13N around 471k/4657k
    assert 470_000 < e < 472_000
    assert 4_656_000 < n < 4_658_000


@_requires_lasio
def test_las_derive_coordinates_falls_back_to_default():
    from app.services.ingest.las_ingester import _derive_coordinates
    e, n = _derive_coordinates("999N999W99", "unknown-hole")
    # Falls back to DEFAULT_UTM_FALLBACK around 480k/4660k
    assert 478_000 < e < 482_000
    assert 4_659_000 < n < 4_661_000


@_requires_lasio
def test_las_parse_las_date_formats():
    from app.services.ingest.las_ingester import _parse_las_date
    import datetime
    assert _parse_las_date("08/13/2012") == datetime.date(2012, 8, 13)
    assert _parse_las_date("2012-08-13") == datetime.date(2012, 8, 13)
    assert _parse_las_date("NA") is None
    assert _parse_las_date("") is None
    assert _parse_las_date(None) is None


# ───────────────────── cameco_log_ingester ────────────────────────

def test_cameco_log_extracts_coords_from_binary_header():
    from app.services.ingest.cameco_log_ingester import parse_cameco_log_header
    import tempfile, os
    # Synthesize a minimal Cameco-format binary header
    synthetic = (
        b"PROCESSED9057C 3.60K   1       F.597923 .10    0.60  340.00UE"
        + b"\x00" * 50
        + b"CAMECO RESOURCES"
        + b"\x00" * 50
        + b"SHIRLEY BASIN"
        + b"\x00" * 30
        + b"E=791126 N=617244"
        + b"\x00" * 100
    )
    with tempfile.NamedTemporaryFile(
        delete=False, suffix="_ORIG.log",
    ) as f:
        # Filename pattern must match the regex: "{digits}-{digits}_{digits}*"
        pass
    new_name = os.path.join(
        os.path.dirname(f.name),
        "36-1042_08-13-12_10-08_9057C_test.log",
    )
    os.rename(f.name, new_name)
    with open(new_name, "wb") as f:
        f.write(synthetic)
    try:
        result = parse_cameco_log_header(new_name)
        assert not result.skipped
        assert result.hole_id == "36-1042"
        assert result.state_plane_easting == 791126.0
        assert result.state_plane_northing == 617244.0
        assert result.basin == "SHIRLEY BASIN"
        assert result.county == "CARBON"
        assert result.state == "WY"
    finally:
        os.unlink(new_name)


def test_cameco_log_skips_when_filename_unmatched():
    from app.services.ingest.cameco_log_ingester import parse_cameco_log_header
    import tempfile, os
    # Filename doesn't match pattern → skipped
    with tempfile.NamedTemporaryFile(
        suffix=".log", delete=False,
    ) as f:
        f.write(b"\x00" * 100)
        fname = f.name
    try:
        result = parse_cameco_log_header(fname)
        assert result.skipped
        assert result.skipped_reason == "filename_pattern_unmatched"
    finally:
        os.unlink(fname)


# ─────────────────────────── pdf_ingester ─────────────────────────

def test_pdf_chunk_pages_basic_paragraph_grouping():
    from app.services.ingest.pdf_ingester import _chunk_pages
    pages = ["Para one is short.\n\nPara two has " + "x" * 200 + " more text."]
    chunks = _chunk_pages(pages)
    assert all(c["text"] for c in chunks)
    # Each chunk has the required metadata
    for c in chunks:
        assert "text" in c
        assert "ordinal" in c
        assert "page_first" in c
        assert "text_hash" in c
        assert len(c["text_hash"]) == 64  # sha256


def test_pdf_chunk_pages_skips_short_pages():
    from app.services.ingest.pdf_ingester import _chunk_pages
    pages = ["x"]  # too short — below MIN_CHUNK
    chunks = _chunk_pages(pages)
    assert chunks == []


def test_pdf_chunk_pages_respects_page_boundary():
    from app.services.ingest.pdf_ingester import _chunk_pages
    pages = ["A" * 500, "B" * 500]
    chunks = _chunk_pages(pages)
    assert len(chunks) >= 2
    # Page 1 chunk doesn't bleed into page 2
    page1_chunks = [c for c in chunks if c["page_first"] == 1]
    page2_chunks = [c for c in chunks if c["page_first"] == 2]
    assert len(page1_chunks) >= 1
    assert len(page2_chunks) >= 1


# ───────────────────────────── kg_sync ────────────────────────────

def test_kg_sync_wyoming_basin_deposit_mapping():
    from app.services.ingest.kg_sync import _WYOMING_BASIN_DEPOSITS
    # Shirley Basin maps to sandstone-hosted roll-front
    assert _WYOMING_BASIN_DEPOSITS["SHIRLEY BASIN"] == "sandstone-hosted roll-front uranium"
    assert "POWDER RIVER BASIN" in _WYOMING_BASIN_DEPOSITS
    assert "WIND RIVER BASIN" in _WYOMING_BASIN_DEPOSITS


# ───────────────────── passage_embedder ───────────────────────────

def test_passage_embedder_point_id_is_passage_id():
    from app.services.ingest.passage_embedder import _passage_to_point_id
    assert _passage_to_point_id("abc-123") == "abc-123"
    # Idempotent: same input → same output
    assert _passage_to_point_id("xyz") == _passage_to_point_id("xyz")


# ─────────────────── tiff_ocr_ingester ────────────────────────────

def test_tiff_ocr_garbage_filter_rejects_short_text():
    from app.services.ingest.tiff_ocr_ingester import _is_garbage_text
    assert _is_garbage_text("")
    assert _is_garbage_text("hi")  # too short
    assert _is_garbage_text(None)


def test_tiff_ocr_garbage_filter_rejects_low_alpha_ratio():
    from app.services.ingest.tiff_ocr_ingester import _is_garbage_text
    # Lots of non-alpha noise
    noise = "1234567890" * 20 + "###@@@$$$%%%" * 10
    assert _is_garbage_text(noise)


def test_tiff_ocr_garbage_filter_accepts_real_text():
    from app.services.ingest.tiff_ocr_ingester import _is_garbage_text
    real = (
        "CENTURY GEOPHYSICAL CORPORATION ORE-GRADE ANALYSIS "
        "COMPANY CAMECO USA WELL 5005-3960 FIELD SHIRLEY BASIN "
        "DATE 08/12/11 K-FACTOR DEAD TIME"
    )
    assert not _is_garbage_text(real)


# ───────────── tiff_ocr chunk-quality filter (Phase F.2) ──────────

def test_chunk_quality_filter_accepts_narrative():
    """Narrative text passes with reasonable stopword density."""
    from app.services.ingest.tiff_ocr_ingester import _chunk_quality_passes_filter
    narrative = (
        "The uranium deposit is hosted in the sandstone of the "
        "Wind River Formation. This is the result of a roll-front "
        "process where reducing groundwater interacted with the "
        "host rock and deposited uranium minerals along the redox "
        "interface. The drill program targets the down-dip extension "
        "of the known mineralization."
    )
    passes, reason = _chunk_quality_passes_filter(narrative)
    # With default 0.0 stopword threshold + 20 vocab min, narrative passes
    assert passes, f"narrative should pass; reason={reason}"


def test_chunk_quality_filter_rejects_low_vocab():
    """Chunk with vocab < FILTER_MIN_VOCAB_SIZE rejected."""
    from app.services.ingest.tiff_ocr_ingester import _chunk_quality_passes_filter
    # Only ~5 unique words, repeated
    tabular = " ".join(["depth"] * 30 + ["gamma"] * 30 + ["grade"] * 30)
    passes, reason = _chunk_quality_passes_filter(tabular)
    assert not passes
    assert reason is not None and reason.startswith("vocab_too_small")


def test_chunk_quality_filter_stopword_threshold_env_driven(monkeypatch):
    """When FILTER_MIN_STOPWORD_RATIO is raised, low-stopword text rejected."""
    # Monkey-patch the module-level constant for the duration of the test
    from app.services.ingest import tiff_ocr_ingester as _tio
    monkeypatch.setattr(_tio, "FILTER_MIN_STOPWORD_RATIO", 0.15)
    # ≥20 unique alpha words so vocab check passes; almost no stopwords
    # so stopword-ratio check fires.
    tabular = " ".join([
        "gamma", "depth", "grade", "cameco", "wyoming", "shirley",
        "basin", "uranium", "ore", "drill", "hole", "section",
        "township", "range", "log", "tool", "probe", "casing",
        "azimuth", "deviation", "survey", "field", "company", "date",
        "fluid", "mud", "rig", "scale", "true", "north",
    ])
    passes, reason = _tio._chunk_quality_passes_filter(tabular)
    assert not passes, f"expected rejection; reason={reason}"
    assert reason is not None and reason.startswith("stopword_ratio_low")


# ───────────────────────── kg_sync regex ──────────────────────────

def test_kg_sync_field_detection_in_project_name():
    """The kg_sync `_WYOMING_BASIN_DEPOSITS` keys should be detectable
    case-insensitively when present in a project name (the sync code's
    `if basin in upper:` pattern)."""
    from app.services.ingest.kg_sync import _WYOMING_BASIN_DEPOSITS
    proj_name_upper = "CAMECO RESOURCES — SHIRLEY BASIN"
    matched = None
    for basin in _WYOMING_BASIN_DEPOSITS:
        if basin in proj_name_upper:
            matched = basin
            break
    assert matched == "SHIRLEY BASIN"
