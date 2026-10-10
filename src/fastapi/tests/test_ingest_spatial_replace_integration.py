"""A spatial re-upload replaces only what it rewrites, against a real Postgres (audit finding 4).

The unit half (the SQL the delete is built from, the refusal when nothing
parsed, the lon/lat check) is test_ingest_spatial_replace_and_bounds.py; this
file runs the same statements against ``silver.spatial_features``.
"""
from __future__ import annotations

# ruff: noqa: F811 - the `project` fixture is imported, then named as a parameter
from types import SimpleNamespace
from typing import Any

import pytest

from app.hatchet_workflows.ingest_spatial import (
    _previous_layers_kept,
    _replace_previous_upload,
    _write_features,
)

# Importing the sibling module also applies its module-level skip when no
# Postgres is configured, and brings the `project` fixture along.
from tests.test_ingest_constraint_rows_integration import (  # noqa: F401 - `project` is a fixture
    _Fixture,
    project,
)

pytestmark = pytest.mark.integration

PREVIOUS = "20260901_100000_geology.zip"
NEW_UPLOAD = "20260929_081500_geology.zip"


async def _seed(project: _Fixture, layer: str | None, n: int = 2, source: str = PREVIOUS) -> None:
    for _ in range(n):
        await project.conn.execute(
            "INSERT INTO silver.spatial_features (feature_id, workspace_id, project_id, "
            "  feature_type, source_file, source_layer) "
            "VALUES (gen_random_uuid(), $1::uuid, $2::uuid, 'boundary', $3, $4)",
            project.workspace_id, project.project_id, source, layer,
        )


async def _layers(project: _Fixture) -> dict[str | None, int]:
    rows = await project.conn.fetch(
        "SELECT source_layer, count(*) AS n FROM silver.spatial_features "
        "WHERE project_id = $1::uuid GROUP BY 1", project.project_id,
    )
    return {r["source_layer"]: r["n"] for r in rows}


@pytest.fixture
async def seeded(project: _Fixture):
    await _seed(project, "faults")
    await _seed(project, "claims", 3)
    await _seed(project, None, 1)
    try:
        yield project
    finally:
        await project.conn.execute(
            "DELETE FROM silver.spatial_features WHERE project_id = $1::uuid",
            project.project_id,
        )


async def test_only_the_layers_this_run_rewrites_are_deleted(seeded: _Fixture) -> None:
    deleted = await _replace_previous_upload(
        seeded.conn, project_id=seeded.project_id, filename=NEW_UPLOAD, layers={"faults"},
    )

    assert deleted == 2
    assert await _layers(seeded) == {"claims": 3, None: 1}


async def test_the_unnamed_layer_is_only_replaced_when_the_run_writes_one(seeded: _Fixture) -> None:
    deleted = await _replace_previous_upload(
        seeded.conn, project_id=seeded.project_id, filename=NEW_UPLOAD, layers={"claims", None},
    )

    assert deleted == 4
    assert await _layers(seeded) == {"faults": 2}


async def test_no_layers_means_nothing_is_deleted(seeded: _Fixture) -> None:
    deleted = await _replace_previous_upload(
        seeded.conn, project_id=seeded.project_id, filename=NEW_UPLOAD, layers=set(),
    )

    assert deleted == 0
    assert await _layers(seeded) == {"faults": 2, "claims": 3, None: 1}


async def test_without_a_layer_list_the_whole_file_is_still_replaced(seeded: _Fixture) -> None:
    deleted = await _replace_previous_upload(
        seeded.conn, project_id=seeded.project_id, filename=NEW_UPLOAD,
    )

    assert deleted == 6 and await _layers(seeded) == {}


async def test_another_file_is_never_touched(seeded: _Fixture) -> None:
    await _seed(seeded, "faults", 4, source="20260901_100000_other.zip")

    await _replace_previous_upload(
        seeded.conn, project_id=seeded.project_id, filename=NEW_UPLOAD,
        layers={"faults", "claims", None},
    )

    assert await _layers(seeded) == {"faults": 4}


async def test_the_layers_left_behind_are_named(seeded: _Fixture) -> None:
    await _replace_previous_upload(
        seeded.conn, project_id=seeded.project_id, filename=NEW_UPLOAD, layers={"faults"},
    )

    kept = await _previous_layers_kept(
        seeded.conn, project_id=seeded.project_id, filename=NEW_UPLOAD,
    )

    assert kept == ["(unnamed layer)", "claims"]


async def test_a_feature_off_the_planet_is_not_stored(project: _Fixture) -> None:
    def feat(name: str, wkt: str) -> SimpleNamespace:
        return SimpleNamespace(
            feature_type="boundary", name=name, properties={}, geometry_wkt=wkt,
        )

    result = SimpleNamespace(
        source_crs="EPSG:4326",
        features=[
            feat("good", "POINT (-105.5 52.1)"),
            feat("projected", "POINT (400797.89 6117305.85)"),
        ],
    )
    warnings: list[dict[str, Any]] = []
    try:
        written = await _write_features(
            project.conn, workspace_id=project.workspace_id, project_id=project.project_id,
            parse_result=result, source_file=NEW_UPLOAD, source_file_sha256=None,
            source_label="zip", layer_override="claims", georef_method="declared",
            crs_confidence=1.0, warnings_out=warnings,
        )
        rows = await project.conn.fetch(
            "SELECT feature_name, ST_X(geom) AS lon FROM silver.spatial_features "
            "WHERE project_id = $1::uuid", project.project_id,
        )
    finally:
        await project.conn.execute(
            "DELETE FROM silver.spatial_features WHERE project_id = $1::uuid",
            project.project_id,
        )

    assert written == 1 and [(r["feature_name"], r["lon"]) for r in rows] == [("good", -105.5)]
    assert [w["code"] for w in warnings] == ["crs_implausible"]
