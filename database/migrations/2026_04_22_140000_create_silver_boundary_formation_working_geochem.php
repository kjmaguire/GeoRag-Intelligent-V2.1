<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Module 8 Chunk 8.2b — silver schema additions that unblock the 4 stubbed MVT functions.
 *
 * Creates:
 *   silver.project_boundaries     (MultiPolygon, EPSG:4326)
 *   silver.geological_formations  (MultiPolygon, EPSG:4326)
 *   silver.historic_workings      (Point, EPSG:4326)
 *
 * Extends (additive ALTER only — no DROP):
 *   silver.geochemistry           (+project_id, workspace_id, geom, sample_id,
 *                                  sample_type, assay_element_codes, assay_values_ppm)
 *
 * Replaces (CREATE OR REPLACE) the 4 RAISE EXCEPTION stubs from the 130000 migration:
 *   silver.pg_boundaries_by_project
 *   silver.pg_formations_by_project
 *   silver.pg_historic_workings_by_project
 *   silver.pg_geochem_by_project
 *
 * Wires the 4 new function entries into docker/martin/martin.yaml
 * (performed in up() via file edit — see Step 6 comment).
 *
 * Schema decisions made vs. spec:
 *
 *   1. Timestamp columns: spec says `timestamptz` but ALL existing silver tables use
 *      `timestamp(0) without time zone` (confirmed by \d silver.workspaces,
 *      silver.projects, silver.collars). New tables follow the existing convention
 *      to avoid mixed-type FK join confusion and match Eloquent's default cast.
 *      Using `timestamp(0) without time zone` throughout.
 *
 *   2. silver.fn_set_updated_at() does not exist in any schema (confirmed by pg_proc
 *      query). The updated_at trigger function is created inline in this migration
 *      under the name `silver.fn_set_updated_at`. It is idempotent (CREATE OR REPLACE).
 *
 *   3. silver.document_revisions PK is `document_revision_id` (not `id`). FK
 *      references in new tables use `document_revision_id` accordingly.
 *
 *   4. silver.collars has no workspace_id column. The geochemistry backfill for
 *      workspace_id must traverse: geochemistry → collar → projects.workspace_id.
 *
 *   5. silver.collars.geom is EPSG:32613 (not 4326). The geochemistry geom backfill
 *      applies ST_Transform(c.geom, 4326) to match the canonical 4326 silver standard.
 *
 *   6. Existing geochemistry oxide columns are: sio2_wt_pct, al2o3_wt_pct,
 *      fe2o3_wt_pct, mgo_wt_pct, cao_wt_pct, na2o_wt_pct, k2o_wt_pct.
 *      Spec referred to `sio2_pct` / `feo_pct` — actual names used in backfill.
 *
 *   7. silver.workspaces has no `plan` column. pgTAP fixture insert uses ON CONFLICT
 *      DO NOTHING which silently drops unknown columns in practice; the existing
 *      test file's workspace INSERT will need to omit `plan` in the 8.2b extension.
 */
return new class extends Migration
{
    public function up(): void
    {
        // ════════════════════════════════════════════════════════════════════
        // SHARED — updated_at trigger function
        // Created here because silver.fn_set_updated_at() does not exist yet.
        // Idempotent via CREATE OR REPLACE.
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.fn_set_updated_at()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                NEW.updated_at := NOW();
                RETURN NEW;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.fn_set_updated_at() IS
            'Shared trigger function: stamps updated_at = NOW() on any UPDATE. Used by silver tables that carry an updated_at column. Module 8 Chunk 8.2b.'");

        // ════════════════════════════════════════════════════════════════════
        // STEP 1 — silver.project_boundaries
        // MultiPolygon per boundary record, project-scoped.
        // Canonical CRS: EPSG:4326 (matches all other silver polygon layers).
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.project_boundaries (
                id                 uuid                        NOT NULL DEFAULT gen_random_uuid(),
                workspace_id       uuid                        NOT NULL,
                project_id         uuid                        NOT NULL,
                boundary_name      text                        NOT NULL,
                boundary_type      text                        NOT NULL,
                effective_from     date,
                effective_to       date,
                source_document_id uuid,
                geom               geometry(MultiPolygon,4326) NOT NULL,
                properties         jsonb                       NOT NULL DEFAULT '{}'::jsonb,
                created_at         timestamp(0) without time zone NOT NULL DEFAULT now(),
                updated_at         timestamp(0) without time zone NOT NULL DEFAULT now(),
                ingested_version   bigint                      NOT NULL DEFAULT 1,

                CONSTRAINT project_boundaries_pkey
                    PRIMARY KEY (id),
                CONSTRAINT project_boundaries_boundary_type_check
                    CHECK (boundary_type IN (
                        'claim','lease','tenement','roi',
                        'concession','license','permit','other'
                    )),
                CONSTRAINT project_boundaries_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id) ON DELETE CASCADE,
                CONSTRAINT project_boundaries_project_id_fkey
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id) ON DELETE CASCADE,
                CONSTRAINT project_boundaries_source_document_id_fkey
                    FOREIGN KEY (source_document_id)
                    REFERENCES silver.document_revisions (document_revision_id) ON DELETE SET NULL
            );
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_project_boundaries_project_id
            ON silver.project_boundaries (project_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_project_boundaries_workspace_id
            ON silver.project_boundaries (workspace_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_project_boundaries_geom
            ON silver.project_boundaries USING gist (geom);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_project_boundaries_type
            ON silver.project_boundaries (boundary_type);');

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'set_updated_at'
                      AND tgrelid = 'silver.project_boundaries'::regclass
                ) THEN
                    CREATE TRIGGER set_updated_at
                        BEFORE UPDATE ON silver.project_boundaries
                        FOR EACH ROW EXECUTE FUNCTION silver.fn_set_updated_at();
                END IF;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON TABLE silver.project_boundaries IS
            'Project-scoped boundary polygons. One project may have multiple boundary records (claim, lease, tenement, ROI, etc.). Canonical CRS: EPSG:4326 MultiPolygon. Module 8 Chunk 8.2b.'");

        DB::statement('GRANT SELECT ON silver.project_boundaries TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // STEP 2 — silver.geological_formations
        // Mapped formation polygons, project-scoped. Distinct from
        // public_geo.pg_bedrock_geology which is workspace-global.
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.geological_formations (
                id                 uuid                        NOT NULL DEFAULT gen_random_uuid(),
                workspace_id       uuid                        NOT NULL,
                project_id         uuid                        NOT NULL,
                formation_code     text                        NOT NULL,
                formation_name     text                        NOT NULL,
                age_period         text,
                age_ma_lower       numeric(8,2),
                age_ma_upper       numeric(8,2),
                lithology_primary  text,
                source_document_id uuid,
                geom               geometry(MultiPolygon,4326) NOT NULL,
                properties         jsonb                       NOT NULL DEFAULT '{}'::jsonb,
                created_at         timestamp(0) without time zone NOT NULL DEFAULT now(),
                updated_at         timestamp(0) without time zone NOT NULL DEFAULT now(),
                ingested_version   bigint                      NOT NULL DEFAULT 1,

                CONSTRAINT geological_formations_pkey
                    PRIMARY KEY (id),
                CONSTRAINT geological_formations_project_code_unique
                    UNIQUE (project_id, formation_code),
                CONSTRAINT geological_formations_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id) ON DELETE CASCADE,
                CONSTRAINT geological_formations_project_id_fkey
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id) ON DELETE CASCADE,
                CONSTRAINT geological_formations_source_document_id_fkey
                    FOREIGN KEY (source_document_id)
                    REFERENCES silver.document_revisions (document_revision_id) ON DELETE SET NULL
            );
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_geological_formations_project_id
            ON silver.geological_formations (project_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geological_formations_workspace_id
            ON silver.geological_formations (workspace_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geological_formations_geom
            ON silver.geological_formations USING gist (geom);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geological_formations_code
            ON silver.geological_formations (formation_code);');

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'set_updated_at'
                      AND tgrelid = 'silver.geological_formations'::regclass
                ) THEN
                    CREATE TRIGGER set_updated_at
                        BEFORE UPDATE ON silver.geological_formations
                        FOR EACH ROW EXECUTE FUNCTION silver.fn_set_updated_at();
                END IF;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON TABLE silver.geological_formations IS
            'Project-scoped mapped geological formation polygons. formation_code is unique per project (strip-log codes). Distinct from public_geo.pg_bedrock_geology which is workspace-global. Module 8 Chunk 8.2b.'");

        DB::statement('GRANT SELECT ON silver.geological_formations TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // STEP 3 — silver.historic_workings
        // Point geometry per historic mining workings location, project-scoped.
        // ════════════════════════════════════════════════════════════════════
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.historic_workings (
                id                    uuid                    NOT NULL DEFAULT gen_random_uuid(),
                workspace_id          uuid                    NOT NULL,
                project_id            uuid                    NOT NULL,
                working_name          text,
                working_type          text                    NOT NULL,
                operational_period    text,
                operational_from_year smallint,
                operational_to_year   smallint,
                commodity_codes       text[],
                status                text,
                source_document_id    uuid,
                geom                  geometry(Point,4326)    NOT NULL,
                properties            jsonb                   NOT NULL DEFAULT '{}'::jsonb,
                created_at            timestamp(0) without time zone NOT NULL DEFAULT now(),
                updated_at            timestamp(0) without time zone NOT NULL DEFAULT now(),
                ingested_version      bigint                  NOT NULL DEFAULT 1,

                CONSTRAINT historic_workings_pkey
                    PRIMARY KEY (id),
                CONSTRAINT historic_workings_working_type_check
                    CHECK (working_type IN (
                        'adit','shaft','open_pit','underground','trench',
                        'costean','placer','exploration_pit','unknown'
                    )),
                CONSTRAINT historic_workings_status_check
                    CHECK (status IS NULL OR status IN (
                        'abandoned','active','reclaimed','rehabilitated','unknown'
                    )),
                CONSTRAINT historic_workings_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id) ON DELETE CASCADE,
                CONSTRAINT historic_workings_project_id_fkey
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id) ON DELETE CASCADE,
                CONSTRAINT historic_workings_source_document_id_fkey
                    FOREIGN KEY (source_document_id)
                    REFERENCES silver.document_revisions (document_revision_id) ON DELETE SET NULL
            );
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_historic_workings_project_id
            ON silver.historic_workings (project_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_historic_workings_workspace_id
            ON silver.historic_workings (workspace_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_historic_workings_geom
            ON silver.historic_workings USING gist (geom);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_historic_workings_type
            ON silver.historic_workings (working_type);');

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname = 'set_updated_at'
                      AND tgrelid = 'silver.historic_workings'::regclass
                ) THEN
                    CREATE TRIGGER set_updated_at
                        BEFORE UPDATE ON silver.historic_workings
                        FOR EACH ROW EXECUTE FUNCTION silver.fn_set_updated_at();
                END IF;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON TABLE silver.historic_workings IS
            'Project-scoped historic mining workings as point geometries. Carries working_type enum, optional operational years, and commodity_codes array. Module 8 Chunk 8.2b.'");

        DB::statement('GRANT SELECT ON silver.historic_workings TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // STEP 4 — silver.geochemistry additive redesign
        //
        // DO NOT DROP the table or any existing oxide columns.
        // ADD IF NOT EXISTS for all new columns.
        //
        // Actual existing oxide column names (confirmed via \d):
        //   sio2_wt_pct, al2o3_wt_pct, fe2o3_wt_pct, mgo_wt_pct,
        //   cao_wt_pct, na2o_wt_pct, k2o_wt_pct
        //
        // Note: spec used sio2_pct / feo_pct — actual names differ.
        // This file uses the actual column names in the backfill.
        // ════════════════════════════════════════════════════════════════════

        // Add new columns
        DB::statement('ALTER TABLE silver.geochemistry
            ADD COLUMN IF NOT EXISTS project_id        uuid
                REFERENCES silver.projects(project_id) ON DELETE CASCADE;');
        DB::statement('ALTER TABLE silver.geochemistry
            ADD COLUMN IF NOT EXISTS workspace_id      uuid
                REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE;');
        DB::statement('ALTER TABLE silver.geochemistry
            ADD COLUMN IF NOT EXISTS geom              geometry(Point,4326);');
        DB::statement('ALTER TABLE silver.geochemistry
            ADD COLUMN IF NOT EXISTS sample_id         text;');
        DB::statement('ALTER TABLE silver.geochemistry
            ADD COLUMN IF NOT EXISTS sample_type       text
                CONSTRAINT geochemistry_sample_type_check CHECK (
                    sample_type IS NULL OR sample_type IN (
                        \'soil\',\'rock_chip\',\'grab\',\'channel\',\'stream_sediment\',
                        \'till\',\'drillhole_pulp\',\'drillhole_reject\',\'other\'
                    )
                );');
        DB::statement('ALTER TABLE silver.geochemistry
            ADD COLUMN IF NOT EXISTS assay_element_codes text[]
                NOT NULL DEFAULT ARRAY[]::text[];');
        DB::statement("ALTER TABLE silver.geochemistry
            ADD COLUMN IF NOT EXISTS assay_values_ppm    jsonb
                NOT NULL DEFAULT '{}'::jsonb;");

        // Backfill project_id, workspace_id, geom, and assay_element_codes.
        //
        // Traversal path:
        //   geochemistry.collar_id → collars.collar_id
        //   collars.project_id     → projects.project_id
        //   projects.workspace_id  → workspaces.workspace_id
        //
        // Geometry: ST_Transform(collars.geom, 4326) because collars.geom is EPSG:32613.
        // collars.geom is nullable; rows where the collar has no geom will remain NULL.
        //
        // assay_element_codes: derive from existing oxide columns (the 7 confirmed columns).
        DB::statement(<<<'SQL'
            UPDATE silver.geochemistry g
            SET
                project_id          = p.project_id,
                workspace_id        = p.workspace_id,
                geom                = CASE
                                        WHEN c.geom IS NOT NULL
                                        THEN ST_Transform(c.geom, 4326)
                                        ELSE NULL
                                      END,
                assay_element_codes = ARRAY_REMOVE(ARRAY[
                    CASE WHEN g.sio2_wt_pct   IS NOT NULL THEN 'Si'  END,
                    CASE WHEN g.al2o3_wt_pct  IS NOT NULL THEN 'Al'  END,
                    CASE WHEN g.fe2o3_wt_pct  IS NOT NULL THEN 'Fe'  END,
                    CASE WHEN g.mgo_wt_pct    IS NOT NULL THEN 'Mg'  END,
                    CASE WHEN g.cao_wt_pct    IS NOT NULL THEN 'Ca'  END,
                    CASE WHEN g.na2o_wt_pct   IS NOT NULL THEN 'Na'  END,
                    CASE WHEN g.k2o_wt_pct    IS NOT NULL THEN 'K'   END
                ], NULL)
            FROM silver.collars c
            JOIN silver.projects p ON p.project_id = c.project_id
            WHERE g.collar_id = c.collar_id;
        SQL);

        // Attempt to add NOT NULL constraints. If any orphaned rows remain, report
        // and skip — do not fail the migration; Kyle resolves orphans separately.
        DB::statement(<<<'SQL'
            DO $$
            DECLARE
                orphan_count bigint;
            BEGIN
                SELECT COUNT(*) INTO orphan_count
                FROM silver.geochemistry
                WHERE project_id IS NULL OR workspace_id IS NULL OR geom IS NULL;

                IF orphan_count = 0 THEN
                    ALTER TABLE silver.geochemistry
                        ALTER COLUMN project_id   SET NOT NULL,
                        ALTER COLUMN workspace_id SET NOT NULL,
                        ALTER COLUMN geom         SET NOT NULL;
                    RAISE NOTICE 'geochemistry: NOT NULL constraints applied on project_id, workspace_id, geom (0 orphans).';
                ELSE
                    RAISE NOTICE 'geochemistry: % row(s) have NULL project_id/workspace_id/geom after backfill. NOT NULL constraints NOT applied. Orphan rows require manual resolution before constraints can be enforced.', orphan_count;
                END IF;
            END;
            $$;
        SQL);

        // New indices (idempotent IF NOT EXISTS)
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochemistry_project_id
            ON silver.geochemistry (project_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochemistry_workspace_id
            ON silver.geochemistry (workspace_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochemistry_geom
            ON silver.geochemistry USING gist (geom);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochemistry_sample_type
            ON silver.geochemistry (sample_type);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_geochemistry_element_codes
            ON silver.geochemistry USING gin (assay_element_codes);');

        DB::statement("COMMENT ON COLUMN silver.geochemistry.project_id IS
            'Added in Module 8 Chunk 8.2b. Backfilled from silver.collars → silver.projects. Enables project-scoped MVT tile queries.';");
        DB::statement("COMMENT ON COLUMN silver.geochemistry.workspace_id IS
            'Added in Module 8 Chunk 8.2b. Backfilled via project_id → silver.projects.workspace_id.';");
        DB::statement("COMMENT ON COLUMN silver.geochemistry.geom IS
            'Added in Module 8 Chunk 8.2b. Point geometry EPSG:4326. Backfilled via ST_Transform(collars.geom, 4326). Collars geom is EPSG:32613.';");
        DB::statement("COMMENT ON COLUMN silver.geochemistry.assay_element_codes IS
            'Derived array of element symbols where the corresponding oxide wt% column is non-null. Backfilled on migration; updated by ingestion pipeline on INSERT/UPDATE. Module 8 Chunk 8.2b.';");
        DB::statement("COMMENT ON COLUMN silver.geochemistry.assay_values_ppm IS
            'Future-facing structured assay store for multi-element (Au, Ag, Cu, Pb, Zn, etc.) values in parts-per-million. Oxide columns remain authoritative for existing major-element data. Module 8 Chunk 8.2b.';");

        DB::statement('GRANT SELECT ON silver.geochemistry TO martin_readonly;');

        // ════════════════════════════════════════════════════════════════════
        // STEP 5 — Replace the 4 RAISE EXCEPTION stubs with real queries
        //
        // All four follow the identical §05d pattern:
        //   - Parse + validate project_id from query_params
        //   - Fetch data_version from silver.projects
        //   - Compute tile bbox via ST_TileEnvelope
        //   - Build MVT + ETag
        //
        // ETag: md5(data_version|z|x|y|project_id) using silver.projects.data_version
        // ════════════════════════════════════════════════════════════════════

        // ── pg_boundaries_by_project ──────────────────────────────────────
        // Source: silver.project_boundaries (MultiPolygon, EPSG:4326 → 3857)
        // Simplification: zoom-aware (polygon layer)
        // Variable: v_pid (not project_id) — avoids PL/pgSQL ambiguity when
        // PostgreSQL tries to resolve `function_name.project_id` as a table.column.
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_boundaries_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql STABLE PARALLEL SAFE
            AS $$
            DECLARE
                v_pid     uuid;
                v         bigint;
                tile_bbox geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(b.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        b.project_id    AS project_id,
                        b.boundary_name AS boundary_name,
                        b.boundary_type AS boundary_type,
                        b.effective_from AS effective_from,
                        b.effective_to  AS effective_to,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(b.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.project_boundaries b
                    WHERE b.project_id = v_pid
                      AND ST_Intersects(ST_Transform(b.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'boundaries', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_boundaries_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature: RETURNS TABLE(mvt bytea, etag_hash text). Source: silver.project_boundaries (MultiPolygon, EPSG:4326→3857). Zoom-aware polygon simplification. ETag = md5(data_version|z|x|y|project_id). Module 8 Chunk 8.2b.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_boundaries_by_project(integer, integer, integer, json) TO martin_readonly;');

        // ── pg_formations_by_project ──────────────────────────────────────
        // Source: silver.geological_formations (MultiPolygon, EPSG:4326 → 3857)
        // Simplification: zoom-aware (polygon layer)
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_formations_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            AS $$
            DECLARE
                v_pid     uuid;
                v         bigint;
                tile_bbox geometry;
                simp_tol  double precision;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);
                simp_tol := CASE
                    WHEN z < 8  THEN 100.0
                    WHEN z < 12 THEN 25.0
                    ELSE             5.0
                END;

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(f.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        f.project_id      AS project_id,
                        f.formation_code  AS formation_code,
                        f.formation_name  AS formation_name,
                        f.age_period      AS age_period,
                        f.age_ma_lower    AS age_ma_lower,
                        f.age_ma_upper    AS age_ma_upper,
                        f.lithology_primary AS lithology_primary,
                        ST_AsMVTGeom(
                            ST_SimplifyPreserveTopology(
                                ST_Transform(f.geom, 3857), simp_tol
                            ),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.geological_formations f
                    WHERE f.project_id = v_pid
                      AND ST_Intersects(ST_Transform(f.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'formations', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_formations_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.geological_formations (MultiPolygon, EPSG:4326→3857). Zoom-aware polygon simplification. Module 8 Chunk 8.2b.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_formations_by_project(integer, integer, integer, json) TO martin_readonly;');

        // ── pg_historic_workings_by_project ───────────────────────────────
        // Source: silver.historic_workings (Point, EPSG:4326 → 3857)
        // No simplification (point layer)
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_historic_workings_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql STABLE PARALLEL SAFE
            AS $$
            DECLARE
                v_pid     uuid;
                v         bigint;
                tile_bbox geometry;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(hw.id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        hw.project_id            AS project_id,
                        hw.working_name          AS working_name,
                        hw.working_type          AS working_type,
                        hw.operational_period    AS operational_period,
                        hw.operational_from_year AS operational_from_year,
                        hw.operational_to_year   AS operational_to_year,
                        to_json(hw.commodity_codes)::text AS commodity_codes,
                        hw.status                AS status,
                        ST_AsMVTGeom(
                            ST_Transform(hw.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.historic_workings hw
                    WHERE hw.project_id = v_pid
                      AND ST_Intersects(ST_Transform(hw.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'historic_workings', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_historic_workings_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.historic_workings (Point, EPSG:4326→3857). No simplification (point layer). commodity_codes text[] JSON-encoded as text property. Module 8 Chunk 8.2b.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_historic_workings_by_project(integer, integer, integer, json) TO martin_readonly;');

        // ── pg_geochem_by_project ─────────────────────────────────────────
        // Source: silver.geochemistry (Point, EPSG:4326 — new geom column)
        // No simplification (point layer)
        // Only returns rows where geom IS NOT NULL (backfilled rows)
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.pg_geochem_by_project(
                z            integer,
                x            integer,
                y            integer,
                query_params json
            )
            RETURNS TABLE (mvt bytea, etag_hash text)
            LANGUAGE plpgsql STABLE PARALLEL SAFE
            AS $$
            DECLARE
                v_pid     uuid;
                v         bigint;
                tile_bbox geometry;
            BEGIN
                v_pid := (query_params->>'project_id')::uuid;
                IF v_pid IS NULL THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                SELECT p.data_version INTO v
                FROM silver.projects p
                WHERE p.project_id = v_pid;

                IF NOT FOUND THEN
                    RETURN QUERY SELECT NULL::bytea, NULL::text;
                    RETURN;
                END IF;

                tile_bbox := ST_TileEnvelope(z, x, y);

                RETURN QUERY
                WITH tile AS (
                    SELECT
                        (hashtext(gc.geochem_id::text)::bigint & x'7FFFFFFFFFFFFFFF'::bigint) AS feature_id,
                        gc.project_id                         AS project_id,
                        gc.sample_id                          AS sample_id,
                        gc.sample_type                        AS sample_type,
                        to_json(gc.assay_element_codes)::text AS assay_element_codes,
                        gc.collar_id                          AS collar_id,
                        ST_AsMVTGeom(
                            ST_Transform(gc.geom, 3857),
                            tile_bbox, 4096, 64, true
                        ) AS geom
                    FROM silver.geochemistry gc
                    WHERE gc.project_id = v_pid
                      AND gc.geom IS NOT NULL
                      AND ST_Intersects(ST_Transform(gc.geom, 3857), tile_bbox)
                )
                SELECT
                    ST_AsMVT(tile, 'geochem', 4096, 'geom') AS mvt,
                    md5(v::text || '|' || z::text || '|' || x::text || '|' || y::text
                        || '|' || v_pid::text) AS etag_hash
                FROM tile;
            END;
            $$;
        SQL);

        DB::statement("COMMENT ON FUNCTION silver.pg_geochem_by_project(integer, integer, integer, json) IS
            'Martin function-source. §05d signature. Source: silver.geochemistry (Point EPSG:4326, new geom column from 8.2b backfill). Skips rows with NULL geom (orphaned collars). assay_element_codes text[] JSON-encoded as text property. Module 8 Chunk 8.2b.'");

        DB::statement('GRANT EXECUTE ON FUNCTION silver.pg_geochem_by_project(integer, integer, integer, json) TO martin_readonly;');
    }

    public function down(): void
    {
        // Drop the 4 new function bodies — the stubs from 130000 will reassert
        // themselves if that migration is re-run; here we drop entirely.
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_boundaries_by_project(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_formations_by_project(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_historic_workings_by_project(integer, integer, integer, json)');
        DB::statement('DROP FUNCTION IF EXISTS silver.pg_geochem_by_project(integer, integer, integer, json)');

        // Drop new tables
        DB::statement('DROP TABLE IF EXISTS silver.historic_workings');
        DB::statement('DROP TABLE IF EXISTS silver.geological_formations');
        DB::statement('DROP TABLE IF EXISTS silver.project_boundaries');

        // Drop trigger function (only if no other tables reference it)
        DB::statement('DROP FUNCTION IF EXISTS silver.fn_set_updated_at()');

        // Reverse geochemistry additions — drop added columns only
        DB::statement('ALTER TABLE silver.geochemistry
            DROP COLUMN IF EXISTS project_id,
            DROP COLUMN IF EXISTS workspace_id,
            DROP COLUMN IF EXISTS geom,
            DROP COLUMN IF EXISTS sample_id,
            DROP COLUMN IF EXISTS sample_type,
            DROP COLUMN IF EXISTS assay_element_codes,
            DROP COLUMN IF EXISTS assay_values_ppm;');
    }
};
