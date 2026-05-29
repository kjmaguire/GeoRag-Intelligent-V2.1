<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * V1.2 schema rename: pg_mineral_occurrence.smdi_id -> external_id.
 *
 * The original column name was modeled on Saskatchewan SMDI vocabulary
 * (Phase 2.1). Phase 4 (BC MINFILE onboarding) revealed the slot is
 * jurisdiction-agnostic — it carries SMDI numbers for CA-SK,
 * MINFILE_NUMBER for CA-BC, MODS IDs for future jurisdictions, and so on.
 * The Phase 4 retrospective flagged the column-name lie for V1.2 rename;
 * this migration carries it out.
 *
 * Same rename applied to the history sibling table so per-feature audit
 * snapshots stay queryable on the new column name.
 *
 * The supporting btree index is renamed too — Postgres `RENAME COLUMN`
 * does not auto-rename indexes that referenced the old column name.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(
            'ALTER TABLE public_geo.pg_mineral_occurrence '
            . 'RENAME COLUMN smdi_id TO external_id'
        );
        DB::statement(
            'ALTER TABLE public_geo.pg_mineral_occurrence_history '
            . 'RENAME COLUMN smdi_id TO external_id'
        );

        // Index rename — keep one concise btree on the new column name.
        DB::statement(
            'ALTER INDEX IF EXISTS public_geo.idx_pg_mineral_occurrence_smdi '
            . 'RENAME TO idx_pg_mineral_occurrence_external_id'
        );

        DB::statement(
            "COMMENT ON COLUMN public_geo.pg_mineral_occurrence.external_id IS "
            . "'Jurisdiction-native external identifier — SMDI number for CA-SK, "
            . "MINFILE_NUMBER for CA-BC, MODS ID for future NL onboarding, etc. "
            . "Renamed from smdi_id in V1.2 to remove the SK-specific name lie.'"
        );

        // Refresh the Martin MVT view so the projected column name matches
        // the renamed source column. PostgreSQL's `CREATE OR REPLACE VIEW`
        // cannot rename a view column even when the underlying table
        // column was renamed (it raises "cannot change name of view
        // column"), so we DROP + CREATE.
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_mineral_occurrences_mvt');
        DB::statement(<<<'SQL'
            CREATE VIEW public_geo.v_pg_mineral_occurrences_mvt AS
            SELECT
                o.id,
                o.jurisdiction_code,
                o.source_id,
                o.source_feature_id,
                o.external_id,
                o.name,
                o.status,
                o.primary_commodities,
                o.associated_commodities,
                o.commodity_grouping,
                o.discovery_type,
                o.production_flag,
                o.source_url,
                o.last_seen_at,
                o.geom
              FROM public_geo.pg_mineral_occurrence o
        SQL);
    }

    public function down(): void
    {
        DB::statement(
            'ALTER TABLE public_geo.pg_mineral_occurrence_history '
            . 'RENAME COLUMN external_id TO smdi_id'
        );
        DB::statement(
            'ALTER TABLE public_geo.pg_mineral_occurrence '
            . 'RENAME COLUMN external_id TO smdi_id'
        );
        DB::statement(
            'ALTER INDEX IF EXISTS public_geo.idx_pg_mineral_occurrence_external_id '
            . 'RENAME TO idx_pg_mineral_occurrence_smdi'
        );
        // Recreate the MVT view with the restored column name so Martin
        // tile serving works after rollback (review finding — the up()
        // DROPs and recreates with external_id; rollback must undo).
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_mineral_occurrences_mvt');
        DB::statement(<<<'SQL'
            CREATE VIEW public_geo.v_pg_mineral_occurrences_mvt AS
            SELECT
                o.id, o.jurisdiction_code, o.source_id, o.source_feature_id,
                o.smdi_id, o.name, o.status,
                o.primary_commodities, o.associated_commodities,
                o.commodity_grouping, o.discovery_type, o.production_flag,
                o.source_url, o.last_seen_at, o.geom
              FROM public_geo.pg_mineral_occurrence o
        SQL);
    }
};
