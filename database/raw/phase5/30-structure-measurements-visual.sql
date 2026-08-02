-- ARCHIVED 2026-07-03 — never applied to any cluster; do NOT run.
--
-- This Phase H4 (doc-phase 186) file has TWO problems, both making it dead:
--
-- 1. gold.structure_measurements_visual here is a different shape than the one
--    that actually shipped:
--      * this file:  PK measurement_id, measurement_kind, depth_m, confidence,
--                    pole_trend_deg/pole_plunge_deg, display_color/display_symbol.
--      * migration 2026_05_13_080002_create_gold_structure_measurements_visual.php
--                    (canonical, live): PK visual_id, structure_type (+ CHECK),
--                    depth, dip_direction_deg/plunge_deg/trend_deg, stereonet_x/_y,
--                    projection. Verified against live 2026-07-03 (16 columns,
--                    PK visual_id). Live stereonet consumers read this migration
--                    shape: the Dagster gold_structure_measurements_visual asset
--                    and src/fastapi/app/services/visualizations/stereonet.py.
--
-- 2. It also CREATEs silver.structure_measurements, which does NOT exist in live
--    (to_regclass NULL, verified 2026-07-02 and again 2026-07-03) and is never
--    created by any migration. The real structural silver table is
--    silver.structures; the gold asset populates from there
--    (src/dagster/.../assets/silver_structure_populate.py). The only remaining
--    references to silver.structure_measurements are dormant doc-strings
--    (visualizations.py Query description for the broken /v1/viz cross_section
--    path, the phase5 visual_readiness probe label, and a couple of
--    architecture docs) — nothing reads or writes the table.
--
-- Both DDLs use CREATE TABLE IF NOT EXISTS, so on a fresh cluster whichever ran
-- first would win and the other would silently no-op. scripts/
-- phase4_step7_build_rollup.sh globs database/raw/phase[0-9]* into
-- current-rollup.sql, so this file could have won the race against the
-- migration and broken the live stereonet path — plus it would have created a
-- phantom silver.structure_measurements table. Archiving it (not just
-- documenting it) removes it from the rollup glob.
--
-- Reviving this shape would require an ADR plus a coordinated rewrite of the
-- Dagster asset and the FastAPI stereonet renderer.

-- Master-plan §5 — gold.structure_measurements_visual
-- Phase H4 (doc-phase 186). Pre-aggregated structural measurements for
-- the stereonet renderer. Each row carries strike + dip in standard
-- right-hand-rule convention plus the original measurement metadata
-- (depth, kind, confidence).

BEGIN;

CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS silver;

-- Silver source — populated by ingest from operator-uploaded structure
-- measurement CSVs. Created here so the gold asset has a FK target.
CREATE TABLE IF NOT EXISTS silver.structure_measurements (
    measurement_id           UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id             UUID            NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    project_id               UUID            NOT NULL REFERENCES silver.projects(project_id) ON DELETE CASCADE,
    collar_id                UUID            REFERENCES silver.collars(collar_id) ON DELETE CASCADE,

    -- Strike-dip values (right-hand rule)
    strike_deg               NUMERIC(6, 3)   NOT NULL,
    dip_deg                  NUMERIC(6, 3)   NOT NULL,

    -- Measurement kind drives stereonet symbology
    measurement_kind         TEXT            NOT NULL,  -- 'bedding' | 'foliation' | 'joint' | 'fault' | 'vein' | 'other'

    -- Location (downhole depth OR surface coords)
    depth_m                  NUMERIC(10, 3),
    easting                  NUMERIC(12, 3),
    northing                 NUMERIC(12, 3),
    crs_epsg                 INTEGER,

    -- Metadata
    confidence               TEXT            DEFAULT 'measured',  -- 'measured' | 'inferred' | 'interpreted'
    notes                    TEXT,
    measured_by              TEXT,
    measured_at              DATE,
    created_at               TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT structure_strike_range CHECK (strike_deg >= 0 AND strike_deg < 360),
    CONSTRAINT structure_dip_range    CHECK (dip_deg >= 0 AND dip_deg <= 90)
);

CREATE INDEX IF NOT EXISTS idx_structure_measurements_project
    ON silver.structure_measurements (project_id, measurement_kind);

CREATE INDEX IF NOT EXISTS idx_structure_measurements_collar
    ON silver.structure_measurements (collar_id);

ALTER TABLE silver.structure_measurements ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS structure_measurements_workspace_isolation
    ON silver.structure_measurements;
CREATE POLICY structure_measurements_workspace_isolation
    ON silver.structure_measurements
    USING (
        workspace_id = current_setting('app.workspace_id', TRUE)::UUID
        OR current_setting('app.workspace_id', TRUE) = ''
    );

GRANT SELECT, INSERT, UPDATE, DELETE ON silver.structure_measurements TO PUBLIC;


-- Gold view — one row per measurement enriched for stereonet rendering.
-- Adds: trend_deg (180° rotation of strike for pole-to-plane plots),
-- plunge_deg (90 - dip), and a stereonet-friendly category label.
CREATE TABLE IF NOT EXISTS gold.structure_measurements_visual (
    measurement_id           UUID            PRIMARY KEY,
    workspace_id             UUID            NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    project_id               UUID            NOT NULL REFERENCES silver.projects(project_id) ON DELETE CASCADE,
    collar_id                UUID            REFERENCES silver.collars(collar_id) ON DELETE CASCADE,

    -- Original (carried verbatim for round-trip)
    strike_deg               NUMERIC(6, 3)   NOT NULL,
    dip_deg                  NUMERIC(6, 3)   NOT NULL,
    measurement_kind         TEXT            NOT NULL,
    depth_m                  NUMERIC(10, 3),
    confidence               TEXT,

    -- Stereonet-ready derived values
    pole_trend_deg           NUMERIC(6, 3)   NOT NULL,  -- strike + 90, normalised [0, 360)
    pole_plunge_deg          NUMERIC(6, 3)   NOT NULL,  -- 90 - dip
    display_color            TEXT,                      -- SME-default per measurement_kind
    display_symbol           TEXT,                      -- 'square' | 'circle' | 'triangle' …

    materialised_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    FOREIGN KEY (measurement_id)
        REFERENCES silver.structure_measurements(measurement_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_structure_measurements_visual_project
    ON gold.structure_measurements_visual (project_id, measurement_kind);

ALTER TABLE gold.structure_measurements_visual ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS structure_visual_workspace_isolation
    ON gold.structure_measurements_visual;
CREATE POLICY structure_visual_workspace_isolation
    ON gold.structure_measurements_visual
    USING (
        workspace_id = current_setting('app.workspace_id', TRUE)::UUID
        OR current_setting('app.workspace_id', TRUE) = ''
    );

GRANT SELECT ON gold.structure_measurements_visual TO PUBLIC;

COMMENT ON TABLE gold.structure_measurements_visual IS
    'Pre-aggregated structural measurements enriched with pole-trend / '
    'pole-plunge for stereonet rendering. Materialised by Dagster '
    'gold_structure_measurements_visual asset.';

COMMIT;
