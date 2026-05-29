-- =============================================================================
-- §19.3 Interpretation Workspace — schema
--
-- Tables let a geologist annotate a project with:
--   - interpretation_notes        freeform per-project notes (optional spatial anchor)
--   - interpretation_section_lines drawn cross-section traces (LineString)
--   - interpretation_target_zones  drawn target polygons (Polygon)
--   - interpretation_comments      threaded comments on any artifact
--
-- All tables carry workspace_id + RLS (tenant scoping).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS interpretation;

-- 1. Notes
CREATE TABLE IF NOT EXISTS interpretation.interpretation_notes (
    note_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL,
    project_id       uuid,
    author_user_id   bigint NOT NULL,
    title            varchar(200),
    body_md          text NOT NULL,
    anchor_geom      geometry(Point, 4326),  -- optional spatial anchor
    tags             text[] NOT NULL DEFAULT ARRAY[]::text[],
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_interp_notes_workspace ON interpretation.interpretation_notes (workspace_id, project_id);
CREATE INDEX IF NOT EXISTS idx_interp_notes_anchor ON interpretation.interpretation_notes USING gist (anchor_geom);
CREATE INDEX IF NOT EXISTS idx_interp_notes_tags ON interpretation.interpretation_notes USING gin (tags);

-- 2. Section lines (drawn cross-section traces)
CREATE TABLE IF NOT EXISTS interpretation.interpretation_section_lines (
    section_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL,
    project_id       uuid,
    author_user_id   bigint NOT NULL,
    name             varchar(200),
    azimuth_deg      numeric(6,2),
    geom             geometry(LineString, 4326) NOT NULL,
    notes            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_interp_section_workspace ON interpretation.interpretation_section_lines (workspace_id, project_id);
CREATE INDEX IF NOT EXISTS idx_interp_section_geom ON interpretation.interpretation_section_lines USING gist (geom);

-- 3. Target zones (drawn polygons)
CREATE TABLE IF NOT EXISTS interpretation.interpretation_target_zones (
    zone_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL,
    project_id       uuid,
    author_user_id   bigint NOT NULL,
    name             varchar(200) NOT NULL,
    rationale        text,
    commodity        varchar(64),
    confidence       varchar(16) NOT NULL DEFAULT 'medium',  -- low|medium|high
    geom             geometry(Polygon, 4326) NOT NULL,
    accepted         boolean NOT NULL DEFAULT FALSE,
    accepted_by      bigint,
    accepted_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT interp_zone_confidence_chk CHECK (confidence IN ('low','medium','high'))
);
CREATE INDEX IF NOT EXISTS idx_interp_zone_workspace ON interpretation.interpretation_target_zones (workspace_id, project_id);
CREATE INDEX IF NOT EXISTS idx_interp_zone_geom ON interpretation.interpretation_target_zones USING gist (geom);

-- 4. Comments (threaded — parent_comment_id self-reference)
CREATE TABLE IF NOT EXISTS interpretation.interpretation_comments (
    comment_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      uuid NOT NULL,
    project_id        uuid,
    author_user_id    bigint NOT NULL,
    parent_comment_id uuid REFERENCES interpretation.interpretation_comments(comment_id) ON DELETE CASCADE,
    target_table      varchar(64) NOT NULL,    -- e.g. interpretation_notes, interpretation_target_zones
    target_id         uuid NOT NULL,
    body_md           text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT interp_comment_target_chk CHECK (
        target_table IN (
            'interpretation_notes',
            'interpretation_section_lines',
            'interpretation_target_zones'
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_interp_comments_target
    ON interpretation.interpretation_comments (target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_interp_comments_thread
    ON interpretation.interpretation_comments (parent_comment_id);

-- =============================================================================
-- RLS — tenant isolation across all 4 tables
-- =============================================================================
ALTER TABLE interpretation.interpretation_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE interpretation.interpretation_section_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE interpretation.interpretation_target_zones ENABLE ROW LEVEL SECURITY;
ALTER TABLE interpretation.interpretation_comments ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT unnest(ARRAY[
            'interpretation_notes',
            'interpretation_section_lines',
            'interpretation_target_zones',
            'interpretation_comments'
        ])
    LOOP
        -- Drop + recreate (idempotent)
        EXECUTE format('DROP POLICY IF EXISTS interp_ws_isolation ON interpretation.%I', t);
        EXECUTE format($f$
            CREATE POLICY interp_ws_isolation ON interpretation.%I
                USING (
                    workspace_id = (
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                    OR NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                )
                WITH CHECK (
                    workspace_id = (
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                )
        $f$, t);
    END LOOP;
END $$;

-- Grant table access to the application role
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
        GRANT USAGE ON SCHEMA interpretation TO georag_app;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON interpretation.interpretation_notes,
               interpretation.interpretation_section_lines,
               interpretation.interpretation_target_zones,
               interpretation.interpretation_comments
            TO georag_app;
    END IF;
END $$;

-- Verify
DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n FROM information_schema.tables
     WHERE table_schema = 'interpretation';
    RAISE NOTICE '§19.3 interpretation schema: % tables created', n;
END $$;
