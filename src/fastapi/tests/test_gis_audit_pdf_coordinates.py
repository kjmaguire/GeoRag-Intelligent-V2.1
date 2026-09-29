"""GIS-18 (audit 2026-09-29): PDF coordinate checks.

* "12S 400000 4000000" is MGRS band S (32-40 N) as readily as the southern
  hemisphere; the hemisphere is stored unknown at half confidence unless
  the northing rules band S out.
* completeness_audit read `lat, lon` from silver.pdf_coordinates, whose
  columns are `latitude, longitude`, so coords_unmappable never fired.
"""
from __future__ import annotations

import inspect

import pytest

from app.services import completeness_audit
from app.services.pdf_coordinates import _extract_from_block, _hemisphere_reading


@pytest.mark.parametrize(("letter", "northing", "expected"), [
    ("N", 6_055_000.0, ("N", 1.0)),
    ("N", 500_000.0, ("N", 1.0)),        # band N is also north
    ("S", 4_000_000.0, (None, 0.5)),     # band S (32-40 N) or ~54 S
    ("S", 7_000_000.0, ("S", 1.0)),      # cannot be band S
])
def test_hemisphere_reading(letter: str, northing: float, expected: tuple) -> None:
    assert _hemisphere_reading(letter, northing) == expected


def test_terse_band_s_match_is_stored_ambiguous() -> None:
    rows = _extract_from_block({"text": "UTM 12S 400000 4000000 (NAD83)"})
    utm = [r for r in rows if r["coord_kind"] == "utm"]
    assert utm and utm[0]["utm_hemisphere"] is None
    assert utm[0]["extraction_confidence"] == 0.5


def test_northern_full_form_is_unchanged() -> None:
    rows = _extract_from_block({"text": "Zone 13N 480500mE 6055000mN"})
    assert rows[0]["utm_hemisphere"] == "N"
    assert rows[0]["extraction_confidence"] == 1.0


def test_completeness_audit_reads_the_writer_column_names() -> None:
    src = inspect.getsource(completeness_audit)
    assert "latitude AS lat" in src and "longitude AS lon" in src
    assert "SELECT page, lat, lon FROM" not in src
