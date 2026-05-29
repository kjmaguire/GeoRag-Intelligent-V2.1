-- =============================================================================
-- §6.6 — gold.h3_density_mineral aggregation table.
--
-- Stores per-(commodity, h3 cell, resolution) counts of mineral occurrences
-- and drillholes from the public_geo.* tables. Refreshed nightly at
-- 05:00 UTC by the gold_h3_density_choropleth Dagster asset.
--
-- Cross-tenant by design (no workspace_id) — public geoscience is shared
-- infrastructure. Exempted in tests/test_tenant_isolation_auditor.py at the
-- same commit as this migration.
--
-- The h3 index is stored as h3index (16-byte unsigned int) for compact
-- storage + native h3_postgis predicate pushdown. Resolution kept as a
-- smallint so a single table holds multiple zoom-band aggregations.
--
-- Idempotent.
-- =============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS gold;

CREATE TABLE IF NOT EXISTS gold.h3_density_mineral (
    commodity_code     varchar(64)    NOT NULL,  -- accommodates 'drillhole'/'unknown' sentinels + multi-element codes
    h3_index           h3index        NOT NULL,
    resolution         smallint       NOT NULL
                       CHECK (resolution BETWEEN 0 AND 15),
    occurrence_count   integer        NOT NULL DEFAULT 0
                       CHECK (occurrence_count >= 0),
    drillhole_count    integer        NOT NULL DEFAULT 0
                       CHECK (drillhole_count >= 0),
    computed_at        timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (commodity_code, h3_index, resolution)
);

-- Resolution + commodity is the typical filter at query time. The
-- composite PK already serves point-lookup; this covers the choropleth
-- scan (one resolution + zero/many commodity codes).
CREATE INDEX IF NOT EXISTS idx_h3_density_resolution_commodity
    ON gold.h3_density_mineral (resolution, commodity_code);

-- h3_index alone for the Martin function's spatial-index style lookup
CREATE INDEX IF NOT EXISTS idx_h3_density_h3
    ON gold.h3_density_mineral (h3_index);

COMMENT ON TABLE gold.h3_density_mineral IS
    '§6.6 — h3 density aggregation of public-geoscience mineral data. '
    'Cross-tenant; refreshed nightly @ 05:00 UTC by '
    'gold_h3_density_choropleth Dagster asset.';

-- Grants — read-only for georag_app (used by Martin function + future
-- admin endpoints). The Dagster asset writes via its own georag
-- credentials.
GRANT USAGE ON SCHEMA gold TO georag_app;
GRANT SELECT ON gold.h3_density_mineral TO georag_app;

COMMIT;
