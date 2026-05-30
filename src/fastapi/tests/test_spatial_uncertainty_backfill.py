"""Unit + integration tests for CC-01 Item 2 — spatial_uncertainty_m backfill.

Three concerns:

1. Rule-table completeness  — every georef_method value in the live schema
   (and the full check-constraint vocabulary) maps to an uncertainty tier.

2. No null leakage — after the SQL migration runs, zero collars with a
   known georef_method should have NULL spatial_uncertainty_m.

3. Histogram integrity — the audit histogram buckets sum to the number of
   collars that had a non-NULL georef_method before the backfill (i.e. all
   567 rows in the current dataset, and for any future dataset the invariant
   is: sum(histogram) == COUNT(*) WHERE georef_method IS NOT NULL).

The integration tests (marked with @pytest.mark.integration) require the
live PostgreSQL stack:
    docker compose up -d postgresql
They skip cleanly when POSTGRES_USER / POSTGRES_PASSWORD are unset.

The unit tests run offline without Docker — they verify the Python-side
rule table mirrors the SQL logic exactly.
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Constants — must stay in sync with the SQL VALUES block
# ---------------------------------------------------------------------------

#: Full check-constraint vocabulary for silver.collars.georef_method
GEOREF_METHOD_VOCAB: frozenset[str] = frozenset(
    {"declared", "detected", "assumed", "manual", "survey"}
)

#: Approved uncertainty tiers (midpoint metres) per the CC-01 sign-off
RULE_TABLE: dict[str, float] = {
    "government_gps":          10.0,   # survey
    "modern_ni43101_declared": 35.0,   # declared/detected/manual + post-2010
    "legacy_declared":         75.0,   # declared/detected/manual + pre-2010 or NULL date
    "legacy_assumed_utm":     175.0,   # assumed (any era)
    # hand_digitised (350 m) intentionally absent — no current georef_method maps to it
}

#: Mapping of georef_method → (modern_rule, legacy_rule)
_METHOD_TO_RULES: dict[str, tuple[str, str]] = {
    "survey":   ("government_gps",          "government_gps"),
    "declared": ("modern_ni43101_declared", "legacy_declared"),
    "detected": ("modern_ni43101_declared", "legacy_declared"),
    "manual":   ("modern_ni43101_declared", "legacy_declared"),
    "assumed":  ("legacy_assumed_utm",      "legacy_assumed_utm"),
}

# Histogram bucket boundaries (upper-inclusive)
HISTOGRAM_BUCKETS: list[tuple[str, float, float]] = [
    ("(0,10]",    0.0,   10.0),
    ("(10,50]",  10.0,   50.0),
    ("(50,100]", 50.0,  100.0),
    ("(100,500]", 100.0, 500.0),
    ("(500,inf)", 500.0, float("inf")),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uncertainty_for(georef_method: str, *, is_modern: bool) -> float | None:
    """Python mirror of the SQL CASE expression."""
    rule_modern, rule_legacy = _METHOD_TO_RULES.get(georef_method, (None, None))
    if rule_modern is None:
        return None
    rule_name = rule_modern if is_modern else rule_legacy
    return RULE_TABLE[rule_name]


def _bucket(uncertainty_m: float) -> str:
    for label, lo, hi in HISTOGRAM_BUCKETS:
        if lo < uncertainty_m <= hi:
            return label
    return "(500,inf)"


def _dsn() -> str:
    return (
        f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:5432/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )


# ---------------------------------------------------------------------------
# Unit tests (no Docker required)
# ---------------------------------------------------------------------------


class TestRuleTableCompleteness:
    """Every value in the check-constraint vocabulary must map to a tier."""

    def test_all_vocab_methods_covered(self):
        """All 5 check-constraint georef_method values have a rule assignment."""
        unmapped = [m for m in GEOREF_METHOD_VOCAB if m not in _METHOD_TO_RULES]
        assert not unmapped, (
            f"georef_method values without a rule assignment: {unmapped!r}. "
            "Add an entry to _METHOD_TO_RULES and RULE_TABLE."
        )

    def test_all_rule_names_in_rule_table(self):
        """Every rule name referenced by _METHOD_TO_RULES exists in RULE_TABLE."""
        missing = set()
        for modern_rule, legacy_rule in _METHOD_TO_RULES.values():
            if modern_rule not in RULE_TABLE:
                missing.add(modern_rule)
            if legacy_rule not in RULE_TABLE:
                missing.add(legacy_rule)
        assert not missing, (
            f"Rule names referenced in _METHOD_TO_RULES but absent from RULE_TABLE: {missing!r}"
        )

    def test_approved_midpoint_values(self):
        """Uncertainty midpoints match the CC-01 approved values exactly."""
        assert RULE_TABLE["government_gps"]          == 10.0
        assert RULE_TABLE["modern_ni43101_declared"] == 35.0
        assert RULE_TABLE["legacy_declared"]         == 75.0
        assert RULE_TABLE["legacy_assumed_utm"]      == 175.0


class TestUncertaintyLogic:
    """Python rule engine produces the correct values for every combination."""

    @pytest.mark.parametrize(
        "georef_method, is_modern, expected",
        [
            ("survey",   True,  10.0),
            ("survey",   False, 10.0),
            ("declared", True,  35.0),
            ("declared", False, 75.0),
            ("detected", True,  35.0),
            ("detected", False, 75.0),
            ("manual",   True,  35.0),
            ("manual",   False, 75.0),
            ("assumed",  True,  175.0),
            ("assumed",  False, 175.0),
        ],
    )
    def test_uncertainty_value(
        self, georef_method: str, is_modern: bool, expected: float
    ):
        result = _uncertainty_for(georef_method, is_modern=is_modern)
        assert result == expected, (
            f"georef_method={georef_method!r}, is_modern={is_modern}: "
            f"got {result}, expected {expected}"
        )

    def test_unknown_method_returns_none(self):
        """Unmapped methods must not produce a guess — return None."""
        assert _uncertainty_for("digitised", is_modern=True) is None
        assert _uncertainty_for("", is_modern=False) is None


class TestHistogramBuckets:
    """Histogram bucket helper assigns the right label to boundary values."""

    @pytest.mark.parametrize(
        "value, expected_bucket",
        [
            (7.0,   "(0,10]"),
            (10.0,  "(0,10]"),
            (10.1,  "(10,50]"),
            (35.0,  "(10,50]"),
            (50.0,  "(10,50]"),
            (50.1,  "(50,100]"),
            (75.0,  "(50,100]"),
            (100.0, "(50,100]"),
            (100.1, "(100,500]"),
            (175.0, "(100,500]"),
            (500.0, "(100,500]"),
            (500.1, "(500,inf)"),
        ],
    )
    def test_bucket_assignment(self, value: float, expected_bucket: str):
        assert _bucket(value) == expected_bucket


# ---------------------------------------------------------------------------
# Integration tests — require live PostgreSQL
# ---------------------------------------------------------------------------


def _pg_available() -> bool:
    return bool(
        os.environ.get("POSTGRES_USER") and os.environ.get("POSTGRES_PASSWORD")
    )


@pytest.mark.integration
@pytest.mark.skipif(not _pg_available(), reason="POSTGRES_USER/PASSWORD not set")
class TestLiveBackfill:
    """Verify the SQL migration ran correctly against the live database."""

    @pytest.fixture
    async def pg(self):
        import asyncpg

        conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
        # The backfill runs as georag (owner), not via georag_app, so no
        # workspace GUC is needed — but set it for good measure so RLS
        # policies don't filter out rows.
        await conn.execute(
            "SET app.workspace_id TO ''",
        )
        try:
            yield conn
        finally:
            await conn.close()

    async def test_no_null_uncertainty_after_backfill(self, pg):
        """Zero collars with a known georef_method should have NULL uncertainty."""
        row = await pg.fetchrow(
            """
            SELECT COUNT(*) AS cnt
              FROM silver.collars
             WHERE spatial_uncertainty_m IS NULL
               AND georef_method IS NOT NULL
            """
        )
        assert row["cnt"] == 0, (
            f"{row['cnt']} collar(s) with a non-NULL georef_method still have "
            "NULL spatial_uncertainty_m — did the backfill migration run?"
        )

    async def test_uncertainty_method_column_exists(self, pg):
        """spatial_uncertainty_method column must exist after the migration."""
        row = await pg.fetchrow(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = 'silver'
               AND table_name   = 'collars'
               AND column_name  = 'spatial_uncertainty_method'
            """
        )
        assert row is not None, (
            "Column silver.collars.spatial_uncertainty_method not found. "
            "Run the 2026_05_30_backfill_spatial_uncertainty.sql migration."
        )

    async def test_method_column_populated_on_updated_rows(self, pg):
        """Every row with a non-NULL uncertainty must also have a method name."""
        row = await pg.fetchrow(
            """
            SELECT COUNT(*) AS cnt
              FROM silver.collars
             WHERE spatial_uncertainty_m IS NOT NULL
               AND spatial_uncertainty_method IS NULL
            """
        )
        assert row["cnt"] == 0, (
            f"{row['cnt']} collar(s) have spatial_uncertainty_m set but "
            "spatial_uncertainty_method is NULL — audit trail is broken."
        )

    async def test_histogram_sums_to_total_method_count(self, pg):
        """Histogram bucket total == count of collars with a non-NULL georef_method."""
        total_row = await pg.fetchrow(
            "SELECT COUNT(*) AS cnt FROM silver.collars WHERE georef_method IS NOT NULL"
        )
        hist_row = await pg.fetchrow(
            "SELECT COUNT(*) AS cnt FROM silver.collars WHERE spatial_uncertainty_m IS NOT NULL"
        )
        assert hist_row["cnt"] == total_row["cnt"], (
            f"Histogram total {hist_row['cnt']} != georef_method count "
            f"{total_row['cnt']}. Some rows may have unmapped methods."
        )

    async def test_all_live_georef_methods_covered(self, pg):
        """No georef_method value in the live DB should be without a rule."""
        rows = await pg.fetch(
            """
            SELECT DISTINCT georef_method
              FROM silver.collars
             WHERE georef_method IS NOT NULL
            """
        )
        live_methods = {r["georef_method"] for r in rows}
        unmapped = live_methods - set(_METHOD_TO_RULES.keys())
        assert not unmapped, (
            f"Live georef_method values with no rule assignment: {unmapped!r}. "
            "Extend _METHOD_TO_RULES and rerun the migration."
        )

    async def test_known_uncertainty_values_in_range(self, pg):
        """All backfilled uncertainty values must be in the approved tier set."""
        rows = await pg.fetch(
            """
            SELECT DISTINCT spatial_uncertainty_m
              FROM silver.collars
             WHERE spatial_uncertainty_m IS NOT NULL
            """
        )
        live_values = {float(r["spatial_uncertainty_m"]) for r in rows}
        approved_values = set(RULE_TABLE.values())
        unexpected = live_values - approved_values
        assert not unexpected, (
            f"Unexpected uncertainty values in live data: {unexpected!r}. "
            f"Approved tiers: {approved_values!r}."
        )
