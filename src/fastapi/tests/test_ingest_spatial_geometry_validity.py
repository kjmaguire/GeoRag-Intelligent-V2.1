"""GIS-11 / GIS-17 (audit 2026-09-29) on the ingest_spatial side.

GIS-17: a self-intersecting polygon was stored as-is and broke
ST_Intersects/ST_Area downstream; it is now repaired, same dimension only,
and reported. GIS-11: a DXF with no EPSG is refused by _crs_refusal (the
parser now returns crs_missing for it).

Imports the Hatchet workflow module: CI env (HATCHET_CLIENT_TOKEN).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.hatchet_workflows import ingest_spatial as sp

_BOWTIE = "POLYGON ((0 0, 10 10, 10 0, 0 10, 0 0))"


def test_valid_geometry_is_untouched() -> None:
    wkt = "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"
    assert sp._repair_invalid_wkt(wkt) == (wkt, None)


def test_bowtie_polygon_is_repaired_to_polygons_only() -> None:
    from shapely import wkt as shapely_wkt

    fixed, reason = sp._repair_invalid_wkt(_BOWTIE)
    assert reason and "Self-intersection" in reason
    geom = shapely_wkt.loads(fixed)
    assert geom.is_valid
    assert geom.geom_type in ("Polygon", "MultiPolygon")
    assert geom.area == pytest.approx(50.0)


def test_unparseable_wkt_passes_through() -> None:
    assert sp._repair_invalid_wkt("NOT WKT") == ("NOT WKT", None)


class _Conn:
    def __init__(self) -> None:
        self.rows: list[tuple[Any, ...]] = []

    async def executemany(self, sql: str, rows: list) -> None:
        self.rows.extend(rows)


@pytest.mark.asyncio
async def test_write_features_repairs_and_reports() -> None:
    feature = SimpleNamespace(
        feature_type="boundary", name="claim A", properties={}, geometry_wkt=_BOWTIE,
    )
    result = SimpleNamespace(source_crs="EPSG:4326", features=[feature])
    conn = _Conn()
    warnings: list[dict[str, Any]] = []

    n = await sp._write_features(
        conn,  # type: ignore[arg-type]
        workspace_id="w", project_id="p", parse_result=result,
        source_file="claims.shp", source_file_sha256=None, source_label="shapefile",
        layer_override="claims", georef_method="declared", crs_confidence=1.0,
        warnings_out=warnings,
    )

    assert n == 1
    assert conn.rows[0][13] != _BOWTIE
    assert [w["code"] for w in warnings] == ["geometry_repaired"]
    assert "claim A" in warnings[0]["detail"]


def test_a_dxf_parsed_without_an_epsg_is_refused() -> None:
    parsed = SimpleNamespace(crs_missing=True, source_crs="", source_file="plan.dxf")
    assert sp._crs_refusal([("plan", parsed)], filename="plan.dxf") is not None
