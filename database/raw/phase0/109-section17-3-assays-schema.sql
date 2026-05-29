-- =============================================================================
-- §17.3 wave 3 — silver.assays + supporting tables
--
-- Unlocks real-data binding for the 4 geochem chart types:
--   Harker diagram   (SiO2 vs major-oxide scatter)
--   Spider diagram   (multi-element pattern)
--   REE pattern      (chondrite-normalized REE)
--   Ternary diagram  (3-component composition)
--
-- Schema mirrors the §22.1.1 spec (silver.assays / silver.assay_samples /
-- silver.assay_quality_flags). Wave 3 ships the data-bearing tables; the
-- QA workflow + outlier-detection are deferred.
-- =============================================================================

-- 1. Sample registry — one row per physical rock/core sample submitted.
CREATE TABLE IF NOT EXISTS silver.assay_samples (
    sample_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL,
    project_id       uuid NOT NULL,
    sample_code      varchar(64) NOT NULL,
    sample_type      varchar(32) NOT NULL DEFAULT 'core',  -- core | grab | chip | pulp | duplicate | blank
    -- Drillhole linkage (optional — surface samples won't have this)
    collar_id        uuid REFERENCES silver.collars(collar_id) ON DELETE SET NULL,
    from_depth_m     numeric(8,2),
    to_depth_m       numeric(8,2),
    -- Surface sample location (optional — drillhole samples won't have this)
    geom             geometry(Point, 4326),
    elevation_m      numeric(8,2),
    -- QA fields
    submitted_at     timestamptz,
    assayed_at       timestamptz,
    lab_code         varchar(64),
    qc_flag          varchar(16) NOT NULL DEFAULT 'ok',  -- ok | suspect | rejected
    rock_type        varchar(64),                          -- for Harker classification
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT assay_sample_type_chk CHECK (
        sample_type IN ('core', 'grab', 'chip', 'pulp', 'duplicate', 'blank')
    ),
    CONSTRAINT assay_sample_qc_chk CHECK (
        qc_flag IN ('ok', 'suspect', 'rejected')
    )
);
CREATE INDEX IF NOT EXISTS idx_assay_samples_workspace
    ON silver.assay_samples (workspace_id, project_id);
CREATE INDEX IF NOT EXISTS idx_assay_samples_collar
    ON silver.assay_samples (collar_id) WHERE collar_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assay_samples_geom
    ON silver.assay_samples USING gist (geom) WHERE geom IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_assay_samples_code
    ON silver.assay_samples (workspace_id, sample_code);

-- 2. Assay results — one row per (sample × element). Tall format so the
-- same table handles ICP-MS (60+ elements) and simple fire-assay (1-2).
CREATE TABLE IF NOT EXISTS silver.assays (
    assay_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL,
    sample_id        uuid NOT NULL REFERENCES silver.assay_samples(sample_id) ON DELETE CASCADE,
    assay_element    varchar(8) NOT NULL,        -- 'Au', 'Cu', 'U', 'SiO2', 'La'…
    assay_value      numeric(20,6),               -- NULL if below detection
    assay_unit       varchar(16) NOT NULL DEFAULT 'ppm',  -- ppm | ppb | pct | g/t
    method_code      varchar(32),                 -- 'AA', 'FA', 'ICP-MS', 'XRF'
    detection_limit  numeric(20,6),
    below_detection  boolean NOT NULL DEFAULT FALSE,
    qc_flag          varchar(16) NOT NULL DEFAULT 'ok',
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT assay_qc_chk CHECK (qc_flag IN ('ok', 'suspect', 'rejected', 'censored')),
    CONSTRAINT assay_unit_chk CHECK (assay_unit IN ('ppm', 'ppb', 'pct', 'g/t', 'oz/t', '%'))
);
CREATE INDEX IF NOT EXISTS idx_assays_sample
    ON silver.assays (sample_id);
CREATE INDEX IF NOT EXISTS idx_assays_workspace_element
    ON silver.assays (workspace_id, assay_element);
-- Full unique index using COALESCE so ON CONFLICT works regardless of method
CREATE UNIQUE INDEX IF NOT EXISTS idx_assays_sample_element
    ON silver.assays (sample_id, assay_element, COALESCE(method_code, ''));

-- =============================================================================
-- RLS — tenant isolation
-- =============================================================================
ALTER TABLE silver.assay_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.assays ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    t text;
BEGIN
    FOR t IN SELECT unnest(ARRAY['assay_samples', 'assays']) LOOP
        EXECUTE format('DROP POLICY IF EXISTS assays_ws_isolation ON silver.%I', t);
        EXECUTE format($f$
            CREATE POLICY assays_ws_isolation ON silver.%I
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

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON silver.assay_samples, silver.assays TO georag_app;
    END IF;
END $$;

-- =============================================================================
-- Seed minimal demo data so the geochem charts have something to render
-- against. ~30 samples × 15 elements = ~450 rows. Synthesised against the
-- first project + a real REE distribution. Idempotent (sample_code UNIQUE
-- WHERE clause → ON CONFLICT).
-- =============================================================================
DO $$
DECLARE
    ws_id uuid := 'a0000000-0000-0000-0000-000000000001';
    proj_id uuid;
    collar uuid;
    sid uuid;
    n int;
    el record;
    samp_code text;
    ree_elements text[] := ARRAY[
        'La','Ce','Pr','Nd','Sm','Eu','Gd','Tb','Dy','Ho','Er','Tm','Yb','Lu'
    ];
    chondrite_ppm jsonb := jsonb_build_object(
        'La', 0.237, 'Ce', 0.612, 'Pr', 0.095, 'Nd', 0.467, 'Sm', 0.153,
        'Eu', 0.058, 'Gd', 0.2055, 'Tb', 0.0374, 'Dy', 0.254, 'Ho', 0.0566,
        'Er', 0.1655, 'Tm', 0.0255, 'Yb', 0.17, 'Lu', 0.0254
    );
    enrichment float;
    val numeric;
BEGIN
    -- Find a real project. If none exist, skip seed.
    SELECT project_id INTO proj_id FROM silver.projects
     WHERE workspace_id = ws_id LIMIT 1;
    IF proj_id IS NULL THEN
        RAISE NOTICE 'silver.assays seed: no project found, skipping demo data';
        RETURN;
    END IF;

    -- Set workspace GUC so RLS accepts the inserts
    PERFORM set_config('app.workspace_id', ws_id::text, false);

    -- 30 samples
    FOR n IN 1..30 LOOP
        samp_code := 'DEMO-SAMPLE-' || lpad(n::text, 4, '0');
        -- Link the first 20 to a real collar if any exist
        SELECT collar_id INTO collar FROM silver.collars
         WHERE project_id = proj_id ORDER BY hole_id OFFSET (n - 1) % 5 LIMIT 1;

        INSERT INTO silver.assay_samples
            (workspace_id, project_id, sample_code, sample_type,
             collar_id, from_depth_m, to_depth_m,
             rock_type, lab_code, assayed_at)
        VALUES
            (ws_id, proj_id, samp_code,
             CASE WHEN n % 7 = 0 THEN 'grab' ELSE 'core' END,
             collar,
             CASE WHEN collar IS NOT NULL THEN ((n * 7) % 200)::numeric(8,2) END,
             CASE WHEN collar IS NOT NULL THEN ((n * 7) % 200 + 1)::numeric(8,2) END,
             (ARRAY['granite','basalt','andesite','rhyolite','schist'])[(n % 5) + 1],
             'DEMO-LAB',
             now() - (n || ' days')::interval)
        ON CONFLICT (workspace_id, sample_code) DO UPDATE
            SET updated_at = now()
        RETURNING sample_id INTO sid;

        -- Add SiO2 + Al2O3 + Au + Cu (for Harker + grade_tonnage)
        INSERT INTO silver.assays (workspace_id, sample_id, assay_element, assay_value, assay_unit, method_code)
        VALUES
            (ws_id, sid, 'SiO2',  (45 + (random() * 30))::numeric(20,6), 'pct',  'XRF'),
            (ws_id, sid, 'Al2O3', (12 + (random() *  6))::numeric(20,6), 'pct',  'XRF'),
            (ws_id, sid, 'Au',    (0.05 + random())::numeric(20,6),       'g/t',  'FA'),
            (ws_id, sid, 'Cu',    (50 + random() * 500)::numeric(20,6),   'ppm',  'ICP-MS')
        ON CONFLICT (sample_id, assay_element, COALESCE(method_code, '')) DO NOTHING;

        -- Add the 14 REE elements (for REE pattern + spider)
        enrichment := 10 + random() * 100;
        FOREACH samp_code IN ARRAY ree_elements LOOP
            val := ((chondrite_ppm ->> samp_code)::float * enrichment)::numeric(20,6);
            INSERT INTO silver.assays (workspace_id, sample_id, assay_element, assay_value, assay_unit, method_code)
            VALUES (ws_id, sid, samp_code, val, 'ppm', 'ICP-MS')
            ON CONFLICT (sample_id, assay_element, COALESCE(method_code, '')) DO NOTHING;
        END LOOP;
    END LOOP;

    RAISE NOTICE 'silver.assays seed: 30 samples × ~18 elements seeded for project %', proj_id;
END $$;

-- Verify
DO $$
DECLARE
    n_samples int; n_assays int;
BEGIN
    PERFORM set_config('app.workspace_id', 'a0000000-0000-0000-0000-000000000001', false);
    SELECT count(*) INTO n_samples FROM silver.assay_samples;
    SELECT count(*) INTO n_assays  FROM silver.assays;
    RAISE NOTICE '§17.3 wave 3: % samples, % assay rows', n_samples, n_assays;
END $$;
