"""§04p native path behaviour tests (master-plan §3 Step 3, doc-phase 51).

Asserts:
- preflight returns valid=True + page count + sha256 on a known native PDF
- profile classifies the PLS-2024 fixture as 'native' at document level
- parse_native produces at least 1 passage with a non-null bbox
- parse_native respects the `pages` argument (paging slice)

The PLS-2024 fixture lives at tests/fixtures/ocr/ (committed; 18 KB).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ocr"
PLS_2024 = FIXTURE_DIR / "PLS-2024-Technical-Report.pdf"


@pytest.fixture(scope="module")
def native_pdf_path() -> Path:
    if not PLS_2024.exists():
        pytest.skip(f"fixture not found: {PLS_2024}")
    return PLS_2024


# ----- preflight -----

def test_preflight_valid_native(native_pdf_path: Path) -> None:
    from app.ocr.preflight import preflight

    result = asyncio.run(preflight(native_pdf_path))

    assert result["valid"] is True
    assert result["page_count"] is not None and result["page_count"] > 0
    assert result["encrypted"] is False
    assert result["sha256"] is not None and len(result["sha256"]) == 64
    assert result["magic_ok"] is True
    assert result["error"] is None


def test_preflight_missing_file(tmp_path: Path) -> None:
    from app.ocr.preflight import preflight

    missing = tmp_path / "does-not-exist.pdf"
    result = asyncio.run(preflight(missing))

    assert result["valid"] is False
    assert result["error"] == "file_not_found"


def test_preflight_non_pdf_magic_mismatch(tmp_path: Path) -> None:
    from app.ocr.preflight import preflight

    not_pdf = tmp_path / "not-a-pdf.pdf"
    not_pdf.write_bytes(b"This is plainly not a PDF.")
    result = asyncio.run(preflight(not_pdf))

    assert result["valid"] is False
    assert result["magic_ok"] is False
    assert result["error"] == "not_a_pdf_magic_mismatch"


# ----- profile -----

def test_profile_classifies_pls_2024_as_native(native_pdf_path: Path) -> None:
    from app.ocr.profile import profile

    result = asyncio.run(profile(native_pdf_path))

    assert result["document_profile"] == "native"
    assert len(result["per_page_profiles"]) == len(result["heuristic_scores"])
    assert all(
        score["text_density"] >= 0.0 for score in result["heuristic_scores"]
    )
    # PLS-2024 is a modern NI 43-101: at least some pages must show
    # text_density above the native threshold.
    from app.ocr.profile import NATIVE_TEXT_DENSITY_MIN

    densities = [s["text_density"] for s in result["heuristic_scores"]]
    assert max(densities) >= NATIVE_TEXT_DENSITY_MIN, (
        f"expected at least one page above NATIVE_TEXT_DENSITY_MIN; got max={max(densities)}"
    )


def test_profile_per_page_profiles_match_count(native_pdf_path: Path) -> None:
    from app.ocr.preflight import preflight
    from app.ocr.profile import profile

    preflight_result = asyncio.run(preflight(native_pdf_path))
    profile_result = asyncio.run(profile(native_pdf_path))

    assert len(profile_result["per_page_profiles"]) == preflight_result["page_count"]


# ----- parse_native -----

def test_parse_native_produces_passages(native_pdf_path: Path) -> None:
    from app.ocr.parse_native import parse_native

    result = asyncio.run(parse_native(native_pdf_path))

    assert result["parser_used"] == "native"
    assert result["page_count"] > 0
    assert len(result["passages"]) > 0
    # Every passage has a 4-element bbox of finite numerics.
    for passage in result["passages"]:
        assert len(passage["bbox"]) == 4
        assert all(isinstance(v, (int, float)) for v in passage["bbox"])
        assert passage["source_method"] == "pdfminer_six"
        assert passage["text_content"].strip() != ""


def test_parse_native_per_page_counts_length(native_pdf_path: Path) -> None:
    from app.ocr.parse_native import parse_native

    result = asyncio.run(parse_native(native_pdf_path))

    assert len(result["per_page_passage_counts"]) == result["page_count"]
    assert len(result["per_page_table_counts"]) == result["page_count"]
    # Sum of per-page counts equals total count
    assert sum(result["per_page_passage_counts"]) == len(result["passages"])
    assert sum(result["per_page_table_counts"]) == len(result["tables"])


def test_parse_native_pages_slice(native_pdf_path: Path) -> None:
    from app.ocr.parse_native import parse_native

    full = asyncio.run(parse_native(native_pdf_path))
    if full["page_count"] < 2:
        pytest.skip("fixture has fewer than 2 pages; cannot test slice")

    first_only = asyncio.run(parse_native(native_pdf_path, pages=[0]))
    # First-page-only should produce a subset of the full passages
    assert all(p["page"] == 0 for p in first_only["passages"])
    assert len(first_only["passages"]) <= len(full["passages"])
