"""§6.2 — unit + integration tests for the BC MINFILE Hatchet pull."""
from __future__ import annotations

import os
from datetime import UTC

import asyncpg
import pytest

from app.hatchet_workflows import bc_minfile_pull as bm
from app.hatchet_workflows import nrcan_geo_pull as ng


# ---------------------------------------------------------------------------
# Workflow registration + schedule contract
# ---------------------------------------------------------------------------
def test_workflow_registered() -> None:
    assert bm.bc_minfile_pull is not None
    assert bm.bc_minfile_pull.name == "bc_minfile_pull"


def test_workflow_in_ai_pool() -> None:
    from app.hatchet_workflows.worker import POOLS
    names = {w.name for w in POOLS["ai"]}
    assert "bc_minfile_pull" in names


# ---------------------------------------------------------------------------
# Input model contracts
# ---------------------------------------------------------------------------
def test_input_defaults_to_both_bc_sources() -> None:
    inp = bm.BcMinfilePullInput()
    assert inp.source_ids == [
        "bc_minfile_mineral_occurrence",
        "bc_minfile_drillhole_collar",
    ]
    assert inp.page_size == 1000


def test_input_page_size_bounded() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        bm.BcMinfilePullInput(page_size=50)  # below 100 floor
    with pytest.raises(ValidationError):
        bm.BcMinfilePullInput(page_size=20_000)  # above 10_000 ceiling


def test_input_empty_source_ids_means_all() -> None:
    """Empty list is the well-defined opt-in to walk every bc_minfile_*
    row in public_geoscience.sources rather than the static default."""
    inp = bm.BcMinfilePullInput(source_ids=[])
    assert inp.source_ids == []


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------
def test_source_pull_result_outcome_values() -> None:
    """The 6 documented outcome strings (completed + 5 failure modes +
    not_registered). The audit-row action_type is `…bc_minfile.<outcome>`,
    so renaming any of these breaks the existing audit-explorer filter."""
    for outcome in [
        "completed",
        "endpoint_unreachable",
        "endpoint_http_error",
        "endpoint_arcgis_error",
        "endpoint_bad_shape",
        "not_registered",
    ]:
        r = bm.SourcePullResult(source_id="x", outcome=outcome)
        assert r.outcome == outcome


def test_pull_output_round_trip() -> None:
    from datetime import datetime
    out = bm.BcMinfilePullOutput(
        sources_attempted=2,
        sources_succeeded=1,
        sources_failed=1,
        per_source=[
            bm.SourcePullResult(source_id="a", outcome="completed", feature_count=15),
            bm.SourcePullResult(source_id="b", outcome="endpoint_arcgis_error"),
        ],
        sampled_at=datetime.now(tz=UTC),
    )
    d = out.model_dump()
    assert d["per_source"][0]["feature_count"] == 15


# ---------------------------------------------------------------------------
# Helper signatures — these are referenced from acceptance harness
# eventually + must stay async + import-stable.
# ---------------------------------------------------------------------------
def test_helpers_are_async() -> None:
    import inspect
    assert inspect.iscoroutinefunction(bm._load_source)
    assert inspect.iscoroutinefunction(bm._fetch_arcgis_page)
    assert inspect.iscoroutinefunction(bm._pull_one_source)


# ---------------------------------------------------------------------------
# DSN builder uses direct-host bypass (no pgbouncer for cron writes)
# ---------------------------------------------------------------------------
def test_dsn_builder_uses_direct_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_USER", "x")
    monkeypatch.setenv("POSTGRES_PASSWORD", "y")
    monkeypatch.setenv("POSTGRES_DIRECT_HOST", "pg-direct")
    monkeypatch.setenv("POSTGRES_DIRECT_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "georag")
    dsn = bm._build_dsn()
    assert "@pg-direct:5433/" in dsn


# ---------------------------------------------------------------------------
# §6.3 — NRCan workflow contract
# ---------------------------------------------------------------------------
def test_nrcan_workflow_registered() -> None:
    assert ng.nrcan_geo_pull is not None
    assert ng.nrcan_geo_pull.name == "nrcan_geo_pull"


def test_nrcan_workflow_in_ai_pool() -> None:
    from app.hatchet_workflows.worker import POOLS
    names = {w.name for w in POOLS["ai"]}
    assert "nrcan_geo_pull" in names


def test_nrcan_input_defaults_to_both_federal_sources() -> None:
    inp = ng.NrcanGeoPullInput()
    assert "nrcan_canadian_mines" in inp.source_ids
    assert "nrcan_geo_bedrock_geology" in inp.source_ids


def test_nrcan_reuses_bc_helpers() -> None:
    """The NRCan module imports its per-source walker from bc_minfile_pull
    rather than duplicating; this contract test pins the import so a
    rename in bc surfaces here too."""
    # NRCan must use the same callable instance (not a copy).
    import app.hatchet_workflows.nrcan_geo_pull as mod
    from app.hatchet_workflows.bc_minfile_pull import _pull_one_source as bc_walker
    assert mod._pull_one_source is bc_walker


# ---------------------------------------------------------------------------
# §6.2 wave 2 — canonical UPSERT path
# ---------------------------------------------------------------------------
def test_canonical_table_for_known_suffixes() -> None:
    """The source_id → canonical_table mapping covers every
    canonical pg_* table currently in scope."""
    cases = [
        ("bc_minfile_mineral_occurrence",  "public_geoscience.pg_mineral_occurrence"),
        ("bc_minfile_drillhole_collar",    "public_geoscience.pg_drillhole_collar"),
        ("sk_mineral_occurrence",          "public_geoscience.pg_mineral_occurrence"),
        ("sk_drillhole_collar",            "public_geoscience.pg_drillhole_collar"),
        ("nrcan_canadian_mines",           "public_geoscience.pg_mine"),
        ("nrcan_geo_bedrock_geology",      "public_geoscience.pg_bedrock_geology"),
        ("bc_aris_assessment_survey",      "public_geoscience.pg_assessment_survey"),
        ("sk_assessment_survey",           "public_geoscience.pg_assessment_survey"),
    ]
    for source_id, expected in cases:
        got = bm._canonical_table_for(source_id)
        assert got == expected, f"{source_id} → {got!r}, expected {expected!r}"


def test_canonical_table_for_unknown_returns_none() -> None:
    assert bm._canonical_table_for("some_made_up_source") is None
    assert bm._canonical_table_for("") is None


def test_normalize_status_maps_to_canonical_enum() -> None:
    """Status normalizer enforces the 7-value CHECK constraint enum."""
    # Exact matches pass through
    assert bm._normalize_status("showing") == "showing"
    assert bm._normalize_status("PROSPECT") == "prospect"  # lowercased
    assert bm._normalize_status("  Producer  ") == "producer"  # trimmed
    # Aliases mapped
    assert bm._normalize_status("Past Producer") == "past-producer"
    assert bm._normalize_status("former producer") == "past-producer"
    assert bm._normalize_status("Mine") == "producer"
    # Empty / None / unknown → 'unknown' fallback (never drop the row)
    assert bm._normalize_status(None) == "unknown"
    assert bm._normalize_status("") == "unknown"
    assert bm._normalize_status("Some weird status nobody has seen") == "unknown"


def test_normalize_status_returns_value_in_canonical_set() -> None:
    """Every output must be one of the 7 enum values."""
    for raw in [None, "", "occurrence", "Past Producer", "garbage", "mine"]:
        assert bm._normalize_status(raw) in bm._CANONICAL_STATUSES


def test_split_commodities_handles_separators() -> None:
    """Upstream commodity field arrives as comma- or pipe-separated;
    the helper normalises to a sorted unique list."""
    assert bm._split_commodities("Au, Cu, Zn") == ["Au", "Cu", "Zn"]
    assert bm._split_commodities("Au|Cu|Zn") == ["Au", "Cu", "Zn"]
    assert bm._split_commodities("Cu, Au, Cu") == ["Au", "Cu"]  # de-duped + sorted
    assert bm._split_commodities("") == []
    assert bm._split_commodities(None) == []
    assert bm._split_commodities(["Au", " Cu ", ""]) == ["Au", "Cu"]


def test_upsert_features_is_async() -> None:
    import inspect
    assert inspect.iscoroutinefunction(bm._upsert_features)


# ---------------------------------------------------------------------------
# UPSERT — PG integration test against a synthetic fixture
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_features_idempotent_against_synthetic_geojson(
    pg_conn: asyncpg.Connection,
) -> None:
    """End-to-end exercise of the UPSERT path using a 2-feature fixture.
    The second run with the same fixture must update in-place
    (rows_touched still = 2; total table count unchanged)."""
    # The bc_minfile_mineral_occurrence source must exist in the
    # registry — it's seeded by the public_geoscience_sources migration.
    src = await bm._load_source(pg_conn, "bc_minfile_mineral_occurrence")
    if src is None:
        pytest.skip("bc_minfile_mineral_occurrence not registered")

    fixture = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-123.5, 50.5]},
            "properties": {
                "MINFILE_NO": "FIXTURE-IT-001",
                "NAME": "Synthetic Occurrence A",
                "STATUS": "Showing",
                "COMMODITIES": "Cu, Au",
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.0, 49.0]},
            "properties": {
                "MINFILE_NO": "FIXTURE-IT-002",
                "NAME": "Synthetic Occurrence B",
                "STATUS": "Prospect",
                "COMMODITIES": "Zn|Pb|Ag",
            },
        },
    ]

    try:
        # First pass
        n1 = await bm._upsert_features(
            pg_conn,
            "public_geoscience.pg_mineral_occurrence",
            src,
            fixture,
        )
        assert n1 == 2
        # Verify rows
        rows = await pg_conn.fetch(
            "SELECT name, primary_commodities FROM public_geoscience.pg_mineral_occurrence "
            "WHERE source_id = $1 AND source_feature_id LIKE 'FIXTURE-IT-%' "
            "ORDER BY source_feature_id",
            "bc_minfile_mineral_occurrence",
        )
        assert len(rows) == 2
        assert rows[0]["name"] == "Synthetic Occurrence A"
        assert rows[0]["primary_commodities"] == ["Au", "Cu"]
        assert rows[1]["primary_commodities"] == ["Ag", "Pb", "Zn"]

        # Second pass — flip status; expect UPDATE not INSERT
        fixture[0]["properties"]["STATUS"] = "Past Producer"
        n2 = await bm._upsert_features(
            pg_conn,
            "public_geoscience.pg_mineral_occurrence",
            src,
            fixture,
        )
        assert n2 == 2
        # Total count unchanged
        total = await pg_conn.fetchval(
            "SELECT count(*) FROM public_geoscience.pg_mineral_occurrence "
            "WHERE source_id = $1 AND source_feature_id LIKE 'FIXTURE-IT-%'",
            "bc_minfile_mineral_occurrence",
        )
        assert total == 2
        # Status updated (and normalised to the canonical enum value)
        new_status = await pg_conn.fetchval(
            "SELECT status FROM public_geoscience.pg_mineral_occurrence "
            "WHERE source_id = $1 AND source_feature_id = 'FIXTURE-IT-001'",
            "bc_minfile_mineral_occurrence",
        )
        # Upstream "Past Producer" → canonical "past-producer"
        assert new_status == "past-producer"
    finally:
        await pg_conn.execute(
            "DELETE FROM public_geoscience.pg_mineral_occurrence "
            "WHERE source_id = $1 AND source_feature_id LIKE 'FIXTURE-IT-%'",
            "bc_minfile_mineral_occurrence",
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_features_skips_features_without_id(
    pg_conn: asyncpg.Connection,
) -> None:
    """A feature missing both MINFILE_NO + OBJECTID + id should be
    skipped silently — we can't UPSERT without a natural key."""
    src = await bm._load_source(pg_conn, "bc_minfile_mineral_occurrence")
    if src is None:
        pytest.skip("bc_minfile_mineral_occurrence not registered")

    fixture = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-123, 50]},
            "properties": {"NAME": "no-id orphan"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122, 49]},
            "properties": {"MINFILE_NO": "FIXTURE-SKIP-WITH-ID"},
        },
    ]
    try:
        n = await bm._upsert_features(
            pg_conn,
            "public_geoscience.pg_mineral_occurrence",
            src,
            fixture,
        )
        # Only the second feature gets upserted; first is skipped silently
        assert n == 1
    finally:
        await pg_conn.execute(
            "DELETE FROM public_geoscience.pg_mineral_occurrence "
            "WHERE source_id = $1 AND source_feature_id LIKE 'FIXTURE-SKIP-%'",
            "bc_minfile_mineral_occurrence",
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_features_other_canonical_tables_skip_gracefully(
    pg_conn: asyncpg.Connection,
) -> None:
    """The wave-2 implementation only supports pg_mineral_occurrence;
    other target tables return 0 instead of crashing. Wave 3 fills in
    pg_drillhole_collar / pg_mine / pg_bedrock_geology / pg_assessment_survey."""
    src = await bm._load_source(pg_conn, "bc_minfile_drillhole_collar")
    if src is None:
        pytest.skip("bc_minfile_drillhole_collar not registered")
    fixture = [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-123, 50]},
        "properties": {"MINFILE_NO": "FIXTURE-SHOULD-NOT-LAND"},
    }]
    n = await bm._upsert_features(
        pg_conn,
        "public_geoscience.pg_drillhole_collar",
        src,
        fixture,
    )
    assert n == 0  # graceful skip, no rows written


# ===========================================================================
# Integration — live stack
# ===========================================================================
PG_DSN = os.environ.get(
    "PG_DSN",
    "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@localhost:5432/georag",
)


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(PG_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_load_source_reads_bc_minfile_row(pg_conn: asyncpg.Connection) -> None:
    """The registered bc_minfile_* sources should resolve via _load_source."""
    src = await bm._load_source(pg_conn, "bc_minfile_mineral_occurrence")
    assert src is not None
    assert src["jurisdiction_code"] == "CA-BC"
    assert src["service_url"]  # non-empty


@pytest.mark.integration
@pytest.mark.asyncio
async def test_load_source_missing_returns_none(pg_conn: asyncpg.Connection) -> None:
    src = await bm._load_source(pg_conn, "nonexistent_source_id")
    assert src is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pull_one_source_handles_arcgis_error_gracefully(
    pg_conn: asyncpg.Connection,
) -> None:
    """The registered BC MINFILE URLs returned an ArcGIS error JSON at
    audit time (DataBC restructured). The workflow must not crash — it
    must return a structured result with outcome=endpoint_arcgis_error."""
    src = await bm._load_source(pg_conn, "bc_minfile_mineral_occurrence")
    if src is None:
        pytest.skip("bc_minfile_mineral_occurrence not registered")
    result = await bm._pull_one_source(pg_conn, src, page_size=100)
    # The exact outcome depends on what BC's server returns right now —
    # acceptable outcomes are: completed (URL works), endpoint_arcgis_error
    # (URL returns ArcGIS error JSON), endpoint_http_error (HTTP non-2xx),
    # endpoint_unreachable (connection fails), endpoint_bad_shape (JSON
    # missing 'features'). Just NOT a crash.
    assert result.outcome in (
        "completed",
        "endpoint_arcgis_error",
        "endpoint_http_error",
        "endpoint_unreachable",
        "endpoint_bad_shape",
    )
    assert result.source_id == "bc_minfile_mineral_occurrence"
    # The duration is always set even on failure paths.
    assert result.duration_s >= 0.0
