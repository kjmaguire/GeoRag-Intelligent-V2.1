"""GIS-16 (audit 2026-09-29): spatial query semantics on 4326 data.

* distance ranking in metres (geography), not degrees;
* dwithin gets an index-assisted bounding-box prefilter;
* the project bbox supplier reads columns that exist (geom_boundary,
  geom_4326) — it used to read `projects.bbox` / `collars.collar_geom`,
  neither of which exists, so it always returned None.
"""
from __future__ import annotations

from app.agent import project_geometry
from app.agent.geospatial_planner import SpatialQuerySpec, plan_spatial_query


def test_distance_orders_by_metres() -> None:
    plan = plan_spatial_query(SpatialQuerySpec(
        target="silver.collars", operation="distance",
        geometry_wkt="POINT(-104.5 59.5)",
    ))
    assert "ORDER BY ST_Distance(geom_4326::geography, " in plan.sql
    assert "4326)::geography)" in plan.sql


def test_dwithin_has_an_index_prefilter_before_the_geography_test() -> None:
    plan = plan_spatial_query(SpatialQuerySpec(
        target="silver.collars", operation="dwithin",
        geometry_wkt="POINT(-104.5 59.5)", buffer_m=1500.0,
    ))
    sql = plan.sql
    assert "geom_4326 && ST_Expand(" in sql
    assert sql.index("geom_4326 && ST_Expand(") < sql.index("ST_DWithin(geom_4326::geography")
    # Same single buffer parameter feeds both.
    assert plan.params == ("POINT(-104.5 59.5)", 1500.0)


def test_public_geoscience_bbox_validation() -> None:
    """GIS-20: an impossible bbox is refused with a usable message."""
    from app.agent.public_geoscience_tool import _bbox_error, _normalize_bbox

    def err(raw):
        return _bbox_error(raw, _normalize_bbox(raw))

    assert err(None) is None
    assert err([-106.0, 57.0, -104.0, 58.0]) is None
    swapped = err([57.0, -106.0, 58.0, -104.0])
    assert swapped and "swapped" in swapped
    assert "greater than maxLat" in err([-106.0, 58.0, -104.0, 57.0])
    assert "antimeridian" in err([179.0, 50.0, -179.0, 52.0])
    assert "four numbers" in err([1, 2, 3])


def test_bbox_supplier_reads_real_4326_columns() -> None:
    project_sql = project_geometry._BBOX_FROM_PROJECT_COLUMN
    collar_sql = project_geometry._BBOX_FROM_COLLARS_ENVELOPE
    assert "geom_boundary" in project_sql and "bbox" not in project_sql.replace("geom_boundary", "")
    assert "geom_4326" in collar_sql
    assert "collar_geom" not in collar_sql
