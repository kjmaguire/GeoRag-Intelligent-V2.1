"""Route-registration regressions found by the orphan-endpoint sweep."""

from __future__ import annotations

from app.main import app


def test_export_routes_do_not_repeat_internal_prefix() -> None:
    paths = set(app.openapi()["paths"])

    assert "/internal/exports/shapefile" in paths
    assert "/internal/exports/geopackage" in paths
    assert not any(path.startswith("/internal/internal/") for path in paths)
