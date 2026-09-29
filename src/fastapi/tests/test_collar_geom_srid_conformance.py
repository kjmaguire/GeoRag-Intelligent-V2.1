"""Every collar writer writes ONE geometry: geom_4326, from the source SRID.

HISTORY
    ``silver.collars.geom`` was created with
    ``AddGeometryColumn('silver', 'collars', 'geom', 32613, 'POINT', 2)`` —
    UTM 13N for every collar on earth — and PostGIS refused anything else:

        InvalidParameterValueError: Geometry SRID (26904) does not match
        column SRID (32613)

    _COLLAR_SQL once inserted the raw source SRID there, so the tabular
    collar write had never worked outside zone 13N. It was then conformed by
    transforming into 32613 at insert, which worked but kept a second,
    zone-pinned geometry beside the WGS84 one.

    Kyle retired the column on 2026-09-29 (§04e, EPSG:4326 at rest;
    2026_09_30_100000_drop_silver_collars_geom). geom_4326 is the only collar
    geometry, transformed at insert straight from the SOURCE CRS, and the
    easting/northing columns keep the untouched source values.

WHAT THIS PINS
    * _COLLAR_SQL, and every other collar INSERT/UPDATE in the ingest
      writers, names no ``geom`` column — a writer still setting it would
      fail with UndefinedColumn on every ingest once the column is dropped.
    * geom_4326 is transformed from the source SRID parameter, not a
      constant, so a project outside the Athabasca default still lands in
      the right place.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

#: Read as SOURCE, not imported: importing app.hatchet_workflows constructs
#: the Hatchet client, which needs HATCHET_CLIENT_TOKEN. These are static
#: SQL contracts, so parsing the files is both sufficient and hermetic.
_APP = Path(__file__).resolve().parents[1] / "app"
_WRITERS: dict[str, Path] = {
    "ingest_tabular": _APP / "hatchet_workflows" / "ingest_tabular.py",
    "cameco_log_ingester": _APP / "services" / "ingest" / "cameco_log_ingester.py",
    "csv_collar_ingester": _APP / "services" / "ingest" / "csv_collar_ingester.py",
    "las_ingester": _APP / "services" / "ingest" / "las_ingester.py",
}

#: A bare `geom` identifier — not geom_4326, not an alias like section_line_geom.
_BARE_GEOM = re.compile(r"(?<![\w.])geom(?!\w)")


def _tree(name: str) -> ast.Module:
    return ast.parse(_WRITERS[name].read_text(encoding="utf-8"))


def _module_str(name: str, target: str) -> str:
    """The string literal module-level *target* is assigned in writer *name*."""
    for node in _tree(name).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == target for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise AssertionError(f"{target} is not a plain string literal in {name}")


def _module_int(name: str, target: str) -> int:
    for node in _tree(name).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == target for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
        ):
            return node.value.value
    raise AssertionError(f"{target} is not an int literal in {name}")


def _collar_write_sql(name: str) -> list[str]:
    """Every string literal in writer *name* that INSERTs into / UPDATEs silver.collars."""
    found: list[str] = []
    for node in ast.walk(_tree(name)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if re.search(r"(INSERT\s+INTO|UPDATE)\s+silver\.collars\b", text):
                found.append(text)
    return found


_COLLAR_SQL = _module_str("ingest_tabular", "_COLLAR_SQL")
DEFAULT_SOURCE_EPSG = _module_int("ingest_tabular", "DEFAULT_SOURCE_EPSG")


def test_collar_sql_writes_no_geom_column() -> None:
    assert not _BARE_GEOM.search(_COLLAR_SQL), (
        "silver.collars.geom was retired; _COLLAR_SQL must write geom_4326 only"
    )


def test_geom_4326_is_transformed_from_the_source_srid() -> None:
    assert re.search(
        r"ST_Transform\(\s*ST_SetSRID\(ST_MakePoint\(\$5, \$6\), \$15::int\),\s*4326\s*\)",
        _COLLAR_SQL,
    ), "geom_4326 must be transformed from the source SRID"
    assert "geom_4326   = EXCLUDED.geom_4326" in _COLLAR_SQL


def test_the_source_epsg_is_still_a_parameter_not_a_constant() -> None:
    # `$15::int` is the project's own CRS and is what makes geom_4326 land in
    # Alaska rather than in Saskatchewan; a constant there would pin every
    # upload to the Athabasca default.
    assert "$15::int" in _COLLAR_SQL
    assert str(DEFAULT_SOURCE_EPSG) not in _COLLAR_SQL


@pytest.mark.parametrize("name", sorted(_WRITERS))
def test_no_collar_writer_names_the_retired_geom_column(name: str) -> None:
    statements = _collar_write_sql(name)
    assert statements, f"{name} has no silver.collars write to check"
    for sql in statements:
        assert not _BARE_GEOM.search(sql), (
            f"{name} still writes silver.collars.geom, which was retired "
            f"2026-09-29:\n{sql}"
        )
        if "INSERT" in sql:
            assert "geom_4326" in sql, f"{name} inserts a collar with no geometry"


@pytest.mark.parametrize("name", sorted(_WRITERS))
def test_no_collar_writer_pins_a_utm_zone(name: str) -> None:
    # The retired column's SRID must not survive as a target anywhere a
    # collar geometry is built.
    for sql in _collar_write_sql(name):
        assert "32613)" not in sql, f"{name} transforms a collar into 32613"
