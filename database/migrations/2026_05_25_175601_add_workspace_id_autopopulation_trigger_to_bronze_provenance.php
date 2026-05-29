<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Auto-populate bronze.provenance.workspace_id from the target silver
 * row, so the 10+ existing INSERT sites (6 Dagster assets + 4 FastAPI
 * services) don't each need to be edited to pass workspace_id
 * explicitly. Landed 2026-05-25 as the follow-up to the bronze RLS
 * migration (2026_05_25_170825) which left the workspace_id column
 * nullable + the policy IS-NULL-exempt.
 *
 * **How it works.** A BEFORE INSERT trigger on bronze.provenance fires
 * only when NEW.workspace_id IS NULL, and resolves the value by
 * looking up the target silver row using
 * (NEW.target_schema, NEW.target_table, NEW.target_id). The lookup
 * supports the six target tables that actually exist + carry
 * workspace_id (mirrors the backfill set in 2026_05_25_170825 + adds
 * geophysics_surveys and assays_v2 for completeness). Targets outside
 * this set leave workspace_id NULL — the RLS IS-NULL exemption keeps
 * them visible until a future migration sweeps them.
 *
 * **Why a trigger, not writer changes.** The 10+ existing INSERT sites
 * are spread across two codebases (Dagster + FastAPI) and would each
 * need their own commit + test. A single PG trigger covers every
 * past, present, and future writer with zero code change. The
 * fallthrough (NEW.workspace_id stays NULL) means the trigger CAN'T
 * cause an INSERT to fail; it can only populate when possible.
 *
 * **Forward compatibility.** When writers ARE updated to pass
 * workspace_id explicitly, the trigger's `IS NULL` guard means it
 * becomes a no-op — no double-write, no policy conflict. This makes
 * the trigger safe to keep forever as a safety net.
 *
 * SQLite (test DB) does not support PL/pgSQL — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION bronze.provenance_autofill_workspace_id()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                resolved_ws uuid;
            BEGIN
                -- Caller already supplied workspace_id explicitly — respect it.
                IF NEW.workspace_id IS NOT NULL THEN
                    RETURN NEW;
                END IF;

                -- Resolve from the target silver row's workspace_id.
                -- One dynamic SQL per known (schema, table) — keeps the
                -- planner happy vs a single giant UNION ALL.
                CASE NEW.target_schema || '.' || NEW.target_table
                    WHEN 'silver.collars' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.collars WHERE collar_id = NEW.target_id;
                    WHEN 'silver.samples' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.samples WHERE sample_id = NEW.target_id;
                    WHEN 'silver.lithology_logs' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.lithology_logs WHERE log_id = NEW.target_id;
                    WHEN 'silver.reports' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.reports WHERE report_id = NEW.target_id;
                    WHEN 'silver.spatial_features' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.spatial_features WHERE feature_id = NEW.target_id;
                    WHEN 'silver.raster_layers' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.raster_layers WHERE raster_id = NEW.target_id;
                    WHEN 'silver.geophysics_surveys' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.geophysics_surveys
                          WHERE survey_id = NEW.target_id;
                    WHEN 'silver.assays_v2' THEN
                        SELECT workspace_id INTO resolved_ws
                          FROM silver.assays_v2 WHERE assay_id = NEW.target_id;
                    ELSE
                        -- Unknown target — leave workspace_id NULL. The
                        -- RLS IS NULL exemption keeps the row visible.
                        RETURN NEW;
                END CASE;

                NEW.workspace_id := resolved_ws;
                RETURN NEW;
            EXCEPTION
                -- Defensive — if the target silver table is missing,
                -- has a different PK column name, or the row doesn't
                -- exist yet (FK ordering quirk), we never want a
                -- provenance INSERT to fail. Trigger swallows + lets
                -- the row land with workspace_id NULL.
                WHEN OTHERS THEN
                    RETURN NEW;
            END;
            $$;
        SQL);

        DB::statement('DROP TRIGGER IF EXISTS provenance_autofill_workspace_id_trg ON bronze.provenance');
        DB::statement(<<<'SQL'
            CREATE TRIGGER provenance_autofill_workspace_id_trg
                BEFORE INSERT ON bronze.provenance
                FOR EACH ROW
                EXECUTE FUNCTION bronze.provenance_autofill_workspace_id()
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP TRIGGER IF EXISTS provenance_autofill_workspace_id_trg ON bronze.provenance');
        DB::statement('DROP FUNCTION IF EXISTS bronze.provenance_autofill_workspace_id()');
    }
};
