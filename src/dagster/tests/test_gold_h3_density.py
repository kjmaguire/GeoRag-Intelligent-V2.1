"""§6.6 — unit tests for the gold_h3_density_choropleth Dagster asset."""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Asset module shape
# ---------------------------------------------------------------------------
def test_asset_imports_clean() -> None:
    """The asset must import without side effects."""
    from georag_dagster.assets.gold_h3_density import gold_h3_density_choropleth
    assert gold_h3_density_choropleth is not None


def test_asset_has_gold_group_name() -> None:
    """Asset belongs to the 'gold' group per Dagster grouping convention."""
    from georag_dagster.assets.gold_h3_density import gold_h3_density_choropleth
    assert gold_h3_density_choropleth.group_names_by_key
    group_names = set(gold_h3_density_choropleth.group_names_by_key.values())
    assert "gold" in group_names


def test_default_resolutions_locked_to_kickoff() -> None:
    """The {5, 7, 9} default resolution set is locked in the kickoff doc;
    if it changes here a kickoff amendment is required."""
    from georag_dagster.assets import gold_h3_density as mod
    assert mod._DEFAULT_RESOLUTIONS == (5, 7, 9)


def test_critical_resolutions_extends_default_with_10() -> None:
    """Critical minerals get one extra band (res 10, ~1.8 km hex) for
    drill-grid alignment. Per kickoff 2026-05-16 geology call."""
    from georag_dagster.assets import gold_h3_density as mod
    assert mod._CRITICAL_RESOLUTIONS == (5, 7, 9, 10)
    # The default set must be a prefix — critical adds, never replaces.
    assert mod._CRITICAL_RESOLUTIONS[:len(mod._DEFAULT_RESOLUTIONS)] == mod._DEFAULT_RESOLUTIONS


def test_critical_mineral_codes_locked() -> None:
    """The 7-commodity critical-mineral list is locked in the kickoff
    doc. Adds/removes need a kickoff amendment + Kyle re-sign-off."""
    from georag_dagster.assets import gold_h3_density as mod
    assert mod._CRITICAL_MINERAL_CODES == frozenset({
        "u", "li", "cu", "co", "ni", "ree", "pge",
    })


def test_critical_codes_are_lowercase() -> None:
    """The codes are matched against LOWER(commodity) in the SQL —
    enforce lowercase here so a typo doesn't silently miss every row."""
    from georag_dagster.assets import gold_h3_density as mod
    for code in mod._CRITICAL_MINERAL_CODES:
        assert code == code.lower(), f"{code!r} must be lowercase"


def test_sql_critical_in_list_is_sorted_quoted_csv() -> None:
    """The SQL helper renders the codes as a sorted, single-quoted,
    comma-separated list. Deterministic ordering keeps the generated
    SQL diff-stable across reruns."""
    from georag_dagster.assets import gold_h3_density as mod
    expected = "'co', 'cu', 'li', 'ni', 'pge', 'ree', 'u'"
    assert mod._sql_critical_in_list() == expected


def test_all_resolutions_union_includes_10() -> None:
    """_ALL_RESOLUTIONS is the union of default + critical; downstream
    consumers (Martin function, MapView toggle) read this as the
    documentation contract for 'which resolutions could appear'."""
    from georag_dagster.assets import gold_h3_density as mod
    assert mod._ALL_RESOLUTIONS == (5, 7, 9, 10)


# ---------------------------------------------------------------------------
# SQL contract — defensive checks against future regression
# ---------------------------------------------------------------------------
def test_aggregate_sql_uses_correct_h3_function() -> None:
    """h3_lat_lng_to_cell is deprecated as of h3_postgis next major;
    asset must use h3_latlng_to_cell."""
    from georag_dagster.assets import gold_h3_density as mod
    assert "h3_latlng_to_cell" in mod._AGGREGATE_SQL
    assert "h3_lat_lng_to_cell" not in mod._AGGREGATE_SQL


def test_aggregate_sql_unnests_primary_commodities_array() -> None:
    """Mineral occurrences carry primary_commodities as text[]; the
    aggregator must unnest to emit one row per (occurrence, commodity)."""
    from georag_dagster.assets import gold_h3_density as mod
    assert "unnest(primary_commodities)" in mod._AGGREGATE_SQL


def test_aggregate_sql_uses_lateral_unnest() -> None:
    """CROSS JOIN LATERAL unnest pattern — without LATERAL the unnest
    isn't correlated to the per-row commodity array."""
    from georag_dagster.assets import gold_h3_density as mod
    assert "CROSS JOIN LATERAL" in mod._AGGREGATE_SQL


def test_aggregate_sql_picks_per_commodity_resolution() -> None:
    """The CASE expression must branch on commodity_code IN (critical list)
    to pick the right resolution array. If this disappears the asset
    silently regresses to uniform {5,7,9} for everything."""
    from georag_dagster.assets import gold_h3_density as mod
    assert "src.commodity_code IN" in mod._AGGREGATE_SQL
    assert "ARRAY[5, 7, 9, 10]::int[]" in mod._AGGREGATE_SQL
    assert "ARRAY[5, 7, 9]::int[]" in mod._AGGREGATE_SQL


def test_aggregate_sql_embeds_every_critical_code() -> None:
    """Each entry in _CRITICAL_MINERAL_CODES must appear in the
    generated SQL's IN-list. Catches a typo / drop in either side."""
    from georag_dagster.assets import gold_h3_density as mod
    for code in mod._CRITICAL_MINERAL_CODES:
        assert f"'{code}'" in mod._AGGREGATE_SQL, f"{code!r} missing from SQL IN-list"


def test_aggregate_sql_filters_invalid_geometries() -> None:
    """ST_IsValid guard prevents the h3 function from crashing on
    broken geometries (rare but possible after a bad ingest)."""
    from georag_dagster.assets import gold_h3_density as mod
    assert "ST_IsValid(geom)" in mod._AGGREGATE_SQL


def test_aggregate_sql_truncates_before_insert() -> None:
    """Full refresh semantics — TRUNCATE before INSERT so old cells
    don't pile up across nightly runs."""
    from georag_dagster.assets import gold_h3_density as mod
    assert "TRUNCATE TABLE gold.h3_density_mineral" in mod._AGGREGATE_SQL


def test_aggregate_sql_drillhole_sentinel_commodity() -> None:
    """Drillholes don't carry a commodity in the canonical schema; they
    contribute to the 'drillhole' sentinel commodity_code."""
    from georag_dagster.assets import gold_h3_density as mod
    assert "'drillhole' AS commodity_code" in mod._AGGREGATE_SQL


# ---------------------------------------------------------------------------
# Tenant-isolation auditor exemption — kickoff-locked cross-tenant
# ---------------------------------------------------------------------------
def test_h3_density_in_workspace_id_exempt() -> None:
    """gold.h3_density_mineral has no workspace_id by design — public
    geoscience is shared infrastructure."""
    # Test lives in src/fastapi/tests but the assertion is on the
    # shared exemption set; spawn the import here so we catch removal.
    import sys, pathlib
    fastapi_tests = pathlib.Path(__file__).parents[2] / "fastapi" / "tests"
    if str(fastapi_tests) not in sys.path:
        sys.path.insert(0, str(fastapi_tests))
    try:
        from test_tenant_isolation_auditor import _WORKSPACE_ID_EXEMPT, _RLS_EXEMPT
    except ImportError:
        pytest.skip("fastapi tests not on import path in this env")
    assert ("gold", "h3_density_mineral") in _WORKSPACE_ID_EXEMPT
    assert ("gold", "h3_density_mineral") in _RLS_EXEMPT
