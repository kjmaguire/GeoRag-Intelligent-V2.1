-- Master-plan §5 (Spatial pipeline + drillhole visuals)
-- doc-phase 185 — Phase H3 strip-log starter
--
-- gold.drillhole_intervals_visual
--   Pre-joined view materialised by the Dagster `gold_drillhole_intervals_visual`
--   asset (lands in a follow-up tick). Each row = one downhole interval
--   already enriched with the collar's spatial context + the corresponding
--   assay points where available. The Plotly strip-log renderer reads this
--   shape directly; no further joins needed at request time.
--
-- Data sources (all silver):
--   silver.collars
--   silver.surveys
--   silver.lithology_intervals
--   silver.assays
--
-- Visual-friendly shape:
--   * `display_color`   — derived from lithology canonical code (SST=yellow,
--                          PGN=red, etc.) — the renderer can fall back to a
--                          default palette if NULL
--   * `display_label`   — short text for the strip-log column (e.g. "SST")
--   * `assay_value_max` — the max assay value across all elements in that
--                          interval, used by the colour-by-grade gradient
--   * `assay_element_max` — which element the max came from
--   * `is_mineralised`  — boolean shortcut for "any assay > project's
--                          mineralisation_threshold"; used to flag intervals
--                          in the strip log

BEGIN;

CREATE SCHEMA IF NOT EXISTS gold;

CREATE TABLE IF NOT EXISTS gold.drillhole_intervals_visual (
    -- Identity
    interval_id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id             UUID            NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    project_id               UUID            NOT NULL REFERENCES silver.projects(project_id) ON DELETE CASCADE,
    collar_id                UUID            NOT NULL REFERENCES silver.collars(collar_id) ON DELETE CASCADE,
    hole_id                  TEXT            NOT NULL,

    -- Depth window
    from_depth_m             NUMERIC(10, 3)  NOT NULL,
    to_depth_m               NUMERIC(10, 3)  NOT NULL,
    interval_length_m        NUMERIC(10, 3)  GENERATED ALWAYS AS (to_depth_m - from_depth_m) STORED,

    -- Lithology
    lithology_code           TEXT,
    lithology_label          TEXT,
    display_label            TEXT,           -- short renderable label (default = lithology_code)
    display_color            TEXT,           -- hex color from the SME palette; renderer falls back when NULL

    -- Assay aggregates (max across all elements in this interval)
    assay_element_max        TEXT,           -- e.g. "U3O8_ppm"
    assay_value_max          NUMERIC(20, 6),
    assay_unit_max           TEXT,
    is_mineralised           BOOLEAN         NOT NULL DEFAULT FALSE,

    -- Spatial context
    easting                  NUMERIC(12, 3),
    northing                 NUMERIC(12, 3),
    elevation_m              NUMERIC(10, 3),
    crs_epsg                 INTEGER,

    -- Audit
    materialised_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    silver_data_version_at_materialisation INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT drillhole_intervals_visual_depth_order
        CHECK (from_depth_m >= 0 AND to_depth_m > from_depth_m),

    CONSTRAINT drillhole_intervals_visual_unique_window
        UNIQUE (collar_id, from_depth_m, to_depth_m)
);

-- Renderer queries: typical access pattern is "give me all intervals for
-- this collar in depth order".
CREATE INDEX IF NOT EXISTS idx_drillhole_intervals_visual_collar_depth
    ON gold.drillhole_intervals_visual (collar_id, from_depth_m);

-- Cross-section / map queries: "all intervals in this project, joined
-- to collar positions for the section line."
CREATE INDEX IF NOT EXISTS idx_drillhole_intervals_visual_project
    ON gold.drillhole_intervals_visual (project_id, workspace_id);

-- Mineralisation-density queries: surface heatmap of "where is the
-- grade above threshold".
CREATE INDEX IF NOT EXISTS idx_drillhole_intervals_visual_mineralised
    ON gold.drillhole_intervals_visual (project_id)
    WHERE is_mineralised = TRUE;

-- RLS — same workspace-scoped policy as every other gold/silver table.
ALTER TABLE gold.drillhole_intervals_visual ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS drillhole_intervals_visual_workspace_isolation
    ON gold.drillhole_intervals_visual;

CREATE POLICY drillhole_intervals_visual_workspace_isolation
    ON gold.drillhole_intervals_visual
    USING (
        workspace_id = current_setting('app.workspace_id', TRUE)::UUID
        OR current_setting('app.workspace_id', TRUE) = ''
    );

GRANT SELECT ON gold.drillhole_intervals_visual TO PUBLIC;

COMMENT ON TABLE gold.drillhole_intervals_visual IS
    'Pre-joined visual-ready strip-log row per drillhole interval. '
    'Materialised by Dagster gold_drillhole_intervals_visual asset. '
    'Source of truth for the §5 Plotly + matplotlib strip-log renderer.';

COMMIT;
