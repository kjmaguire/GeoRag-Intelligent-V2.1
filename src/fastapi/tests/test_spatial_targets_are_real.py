"""Every spatial target must name a column the database actually has.

Two of the four did not, and both failed in the same invisible way.
geospatial_planner builds SQL from SPATIAL_TARGETS; Postgres answers
UndefinedColumn or UndefinedTable; tools_geospatial catches it and returns
None; the orchestrator drops a None tool result and carries on. A user
asking a spatial question got a confident answer built from everything
EXCEPT the spatial data, with nothing anywhere saying a table was missing.

  silver.collars   declared `collar_geom`. It exists in no migration and no
                   raw file. The real columns were `geom` (EPSG:32613,
                   retired 2026-09-29) and `geom_4326`, which every other
                   production path already read.

  gold.h3_density  declared table and column both fictional. The real table
                   is gold.h3_density_mineral and it has no geometry at all
                   -- location is an h3index. Removed rather than repaired.

The second one is why this file checks the CRS too. The obvious repair for
collars was `collar_geom` -> `geom`, and that would have been worse than the
bug: plan_spatial_query compares spec.crs_epsg against the target's DECLARED
crs_epsg, so a target claiming 4326 over a 32613 column waves every spec
through and evaluates the predicate in the wrong units. A silent wrong
answer instead of a silent empty one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.geospatial_planner import SPATIAL_TARGETS

REPO = Path(__file__).resolve().parents[3]
DATABASE = REPO / "database"


def _database_text() -> str:
    """Every migration and raw SQL file, concatenated once."""
    parts: list[str] = []
    for path in sorted(DATABASE.rglob("*")):
        if path.suffix in {".php", ".sql"} and path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def database_sql() -> str:
    if not DATABASE.is_dir():  # pragma: no cover - checkout without database/
        pytest.skip("database/ not present in this checkout")
    return _database_text()


@pytest.mark.parametrize("name", sorted(SPATIAL_TARGETS))
def test_the_geometry_column_exists_somewhere_in_the_schema(
    name: str, database_sql: str
) -> None:
    """Deliberately loose: presence, not a DDL parse.

    Geometry columns arrive three different ways here -- AddGeometryColumn,
    Schema::create, and raw CREATE TABLE -- so parsing them properly would
    be brittle for no gain. A name that appears NOWHERE is the failure that
    actually happened, and that is what this catches.
    """
    target = SPATIAL_TARGETS[name]
    assert target.geom_column in database_sql, (
        f"{name} declares geom_column={target.geom_column!r}, which appears "
        f"in no migration and no raw SQL file. Every plan built against this "
        f"target raises UndefinedColumn, which tools_geospatial swallows into "
        f"a None result -- so the tool reports nothing found rather than "
        f"nothing working."
    )


@pytest.mark.parametrize("name", sorted(SPATIAL_TARGETS))
def test_the_table_exists_somewhere_in_the_schema(
    name: str, database_sql: str
) -> None:
    bare = name.split(".", 1)[-1]
    assert bare in database_sql, (
        f"{name} names a table that appears nowhere in database/. "
        f"gold.h3_density was exactly this: the real table is "
        f"gold.h3_density_mineral, and it has no geometry column to target."
    )


def test_collars_does_not_declare_4326_over_the_32613_column() -> None:
    """The repair that would have been worse than the bug.

    silver.collars.geom was EPSG:32613 (2026_04_09_180100 created it with
    AddGeometryColumn(..., 32613)) until it was retired on 2026-09-29
    (2026_09_30_100000_drop_silver_collars_geom). Pointing this target at
    `geom` with crs_epsg=4326 turned a swallowed UndefinedColumn into a
    silent wrong answer; now `geom` does not exist at all, so geom_4326 /
    4326 is the only correct declaration.
    """
    target = SPATIAL_TARGETS["silver.collars"]

    assert target.geom_column == "geom_4326"
    assert target.crs_epsg == 4326


def test_the_h3_target_is_gone_and_stays_gone() -> None:
    """It cannot be repaired in place, so it must not come back by name."""
    assert "gold.h3_density" not in SPATIAL_TARGETS

    from app.agent.tools_geospatial import _TARGET_KEYWORDS

    routed = {target for _pattern, target in _TARGET_KEYWORDS}
    assert "gold.h3_density" not in routed, (
        "a keyword route to a target that does not exist raises KeyError in "
        "plan_spatial_query, which is swallowed the same way"
    )
    assert routed <= set(SPATIAL_TARGETS), (
        f"these keyword routes point at targets that do not exist: "
        f"{sorted(routed - set(SPATIAL_TARGETS))}"
    )
