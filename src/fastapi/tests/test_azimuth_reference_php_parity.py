"""The Workspace 3D azimuth conversion (PHP) agrees with promote (Python).

GIS audit 2026-10, finding 5. ``App\\Services\\Collars\\SurveyAzimuthReference``
converts a DECLARED azimuth reference to an azimuth from true north for the 3D
frame; ``promote_silver_to_gold`` applies the same declaration (via
``app.services.ingest.azimuth_reference``) to build the map trace. They are one
rule in two languages, so this file pins them together the way
``HoleIdTest`` pins the hole-id rule:

* the PHP spelling table must equal ``georag_geoparsers._azimuth_reference``;
* for a fixed collar the true-north azimuth the PHP class must produce is
  computed here FROM THE PYTHON MODULE (a station's grid azimuth in the
  collar's own zone, then back to true north) and compared with the literals
  in ``tests/Unit/Services/Collars/SurveyAzimuthReferenceTest.php``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from georag_geoparsers import _azimuth_reference as spellings

from app.hatchet_workflows.promote_silver_to_gold import _collar_local_utm
from app.services.ingest.azimuth_reference import (
    apply,
    azimuth_correction,
    true_north_bearing_in_grid,
)

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parents[-1]
PHP_CLASS = REPO_ROOT / "app" / "Services" / "Collars" / "SurveyAzimuthReference.php"

_needs_php_source = pytest.mark.skipif(
    not PHP_CLASS.exists(),
    reason="app/ is not mounted (container run of src/fastapi only)",
)

# The fixture collar of SurveyAzimuthReferenceTest.php.
LON, LAT = -102.0, 58.0


def _php_spellings() -> dict[str, frozenset[str]]:
    text = PHP_CLASS.read_text()
    block = re.search(r"const SPELLINGS = \[(.*?)\n    \];", text, re.DOTALL)
    assert block, "SPELLINGS constant not found in the PHP class"
    out: dict[str, frozenset[str]] = {}
    for key, body in re.findall(r"self::([A-Z]+) => \[(.*?)\]", block.group(1), re.DOTALL):
        out[key.lower()] = frozenset(re.findall(r"'([a-z_]+)'", body))
    return out


@_needs_php_source
def test_the_php_spellings_are_the_python_spellings() -> None:
    assert _php_spellings() == {k: frozenset(v) for k, v in spellings._SPELLINGS.items()}


@_needs_php_source
def test_the_php_constants_are_the_stored_values() -> None:
    text = PHP_CLASS.read_text()
    for name, value in (("TRUE", spellings.TRUE), ("MAGNETIC", spellings.MAGNETIC), ("GRID", spellings.GRID)):
        assert re.search(rf"const {name} = '{value}';", text), f"{name} must be {value!r}"


def _map_true_azimuth(azimuth: float, reference: str | None, declination: float | None, project_epsg: int | None) -> float:
    """The direction promote's trace points on the map, as an azimuth from TRUE north.

    promote builds the trace in the collar's own UTM zone at the corrected
    (grid) azimuth; a direction at grid azimuth g points at true azimuth
    g - theta, theta being the bearing of true north in that grid.
    """
    local_epsg = _collar_local_utm(LON, LAT)
    correction = azimuth_correction(
        orientation_reference=reference,
        magnetic_declination=declination,
        project_epsg=project_epsg,
        local_epsg=local_epsg,
        lon=LON,
        lat=LAT,
    )
    grid_azimuth = apply(azimuth, correction)
    return (grid_azimuth - true_north_bearing_in_grid(local_epsg, LON, LAT)) % 360.0


def test_the_fixture_collar_is_in_utm_14n_with_the_literal_convergence() -> None:
    assert _collar_local_utm(LON, LAT) == 32614
    assert true_north_bearing_in_grid(32614, LON, LAT) == pytest.approx(2.544802210, abs=1e-8)
    assert true_north_bearing_in_grid(26913, LON, LAT) == pytest.approx(-2.544802210, abs=1e-8)


# (reference, declination, project EPSG, expected 3D azimuth for a station recorded at 90)
# The expected values are the literals in SurveyAzimuthReferenceTest.php.
@pytest.mark.parametrize(
    "reference,declination,project_epsg,expected",
    [
        ("true", None, None, 90.0),
        ("magnetic", 12.0, None, 102.0),
        ("grid", None, None, 87.455197790),     # the collar's own zone
        ("grid", None, 32614, 87.455197790),    # a project CRS equal to the zone changes nothing
        ("grid", None, 26913, 92.544802210),    # a different projection
    ],
)
def test_the_true_north_azimuth_php_must_produce(
    reference: str, declination: float | None, project_epsg: int | None, expected: float,
) -> None:
    assert _map_true_azimuth(90.0, reference, declination, project_epsg) == pytest.approx(expected, abs=1e-8)


def test_the_undeclared_residual_is_exactly_the_local_convergence() -> None:
    """Pins what the 3D frame does NOT reconcile, so changing it is a decision.

    A station with no declared reference is drawn in 3D at its recorded
    azimuth read as TRUE north. promote reads the same number as grid north of
    the collar's UTM zone, so the map trace points at (recorded - theta).
    Kyle (2026-09-29): no correction unless the data declares its reference.
    """
    on_the_map = _map_true_azimuth(90.0, None, None, None)
    in_3d = 90.0
    assert in_3d - on_the_map == pytest.approx(true_north_bearing_in_grid(32614, LON, LAT), abs=1e-8)
    assert abs(in_3d - on_the_map) > 2.0, "about 2.5 degrees here: not nothing, and not hidden"
