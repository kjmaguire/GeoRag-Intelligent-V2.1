-- Master-plan §5 — gold.cross_section_panels
-- Phase H4 (doc-phase 186). Pre-projects every drillhole interval onto a
-- named section line so the cross-section renderer can produce 2-D
-- elevation × distance-along-line panels without re-computing geometry
-- per request.
--
-- Pipeline (materialised by Dagster gold_cross_section_panels asset):
--   1. Read silver.section_lines (named cross-section linestrings the
--      operator drew, e.g. "AB through PLS-22-08" / "Pleistocene N-S
--      transect"). EPSG:4326.
--   2. For every collar within `corridor_buffer_m` of the line, project
--      its (easting, northing) onto the line → distance_along_m.
--   3. For every interval on that collar, compute the 2-D vertical
--      polygon: (distance_along_m, top_elevation_m) to
--      (distance_along_m + interval_thickness_m × cos_dip, bottom_elevation_m).
--   4. Carry over lithology + mineralisation flags from
--      gold.drillhole_intervals_visual so the cross-section can re-use
--      the same SME palette.

BEGIN;

CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS silver;

-- Section lines are operator-drawn. Created here so the gold table FK
-- has a target even before the §5 admin UI for drawing sections lands.
CREATE TABLE IF NOT EXISTS silver.section_lines (
    section_line_id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id             UUID            NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    project_id               UUID            NOT NULL REFERENCES silver.projects(project_id) ON DELETE CASCADE,
    name                     TEXT            NOT NULL,
    description              TEXT,
    corridor_buffer_m        NUMERIC(8, 2)   NOT NULL DEFAULT 250.0,
    geom                     geometry(LineString, 4326) NOT NULL,
    created_by_user_id       INTEGER,
    created_at               TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT section_line_corridor_positive CHECK (corridor_buffer_m > 0)
);

CREATE INDEX IF NOT EXISTS idx_section_lines_geom
    ON silver.section_lines USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_section_lines_project
    ON silver.section_lines (project_id);

ALTER TABLE silver.section_lines ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS section_lines_workspace_isolation ON silver.section_lines;
CREATE POLICY section_lines_workspace_isolation
    ON silver.section_lines
    USING (
        workspace_id = current_setting('app.workspace_id', TRUE)::UUID
        OR current_setting('app.workspace_id', TRUE) = ''
    );

GRANT SELECT, INSERT, UPDATE, DELETE ON silver.section_lines TO PUBLIC;

-- Cross-section panels (the actual gold table)
CREATE TABLE IF NOT EXISTS gold.cross_section_panels (
    panel_id                 UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id             UUID            NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    project_id               UUID            NOT NULL REFERENCES silver.projects(project_id) ON DELETE CASCADE,
    section_line_id          UUID            NOT NULL REFERENCES silver.section_lines(section_line_id) ON DELETE CASCADE,

    -- One panel = one interval projected onto the line.
    interval_id              UUID            NOT NULL REFERENCES gold.drillhole_intervals_visual(interval_id) ON DELETE CASCADE,
    collar_id                UUID            NOT NULL REFERENCES silver.collars(collar_id) ON DELETE CASCADE,
    hole_id                  TEXT            NOT NULL,

    -- 2-D polygon vertices in (distance-along-line × elevation) space.
    distance_along_m         NUMERIC(10, 3)  NOT NULL,
    top_elevation_m          NUMERIC(10, 3)  NOT NULL,
    bottom_elevation_m       NUMERIC(10, 3)  NOT NULL,
    panel_width_m            NUMERIC(8, 3)   NOT NULL DEFAULT 5.0,  -- visual width of the column

    -- Lithology + mineralisation flag carried over from the visual table.
    lithology_code           TEXT,
    display_label            TEXT,
    display_color            TEXT,
    is_mineralised           BOOLEAN         NOT NULL DEFAULT FALSE,
    assay_value_max          NUMERIC(20, 6),
    assay_element_max        TEXT,

    -- Distance perpendicular to the line; >0 to one side, <0 to the other.
    perpendicular_offset_m   NUMERIC(8, 3)   NOT NULL,

    materialised_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT panel_elevation_order
        CHECK (top_elevation_m >= bottom_elevation_m),
    CONSTRAINT panel_unique_per_section
        UNIQUE (section_line_id, interval_id)
);

CREATE INDEX IF NOT EXISTS idx_cross_section_panels_section_line
    ON gold.cross_section_panels (section_line_id, distance_along_m);

CREATE INDEX IF NOT EXISTS idx_cross_section_panels_collar
    ON gold.cross_section_panels (collar_id);

ALTER TABLE gold.cross_section_panels ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS cross_section_panels_workspace_isolation
    ON gold.cross_section_panels;
CREATE POLICY cross_section_panels_workspace_isolation
    ON gold.cross_section_panels
    USING (
        workspace_id = current_setting('app.workspace_id', TRUE)::UUID
        OR current_setting('app.workspace_id', TRUE) = ''
    );

GRANT SELECT ON gold.cross_section_panels TO PUBLIC;

COMMENT ON TABLE gold.cross_section_panels IS
    'Pre-projected 2-D cross-section panel per (section_line, interval). '
    'Materialised by Dagster gold_cross_section_panels asset. Source of '
    'truth for the §5 Plotly + matplotlib cross-section renderer.';

COMMIT;
