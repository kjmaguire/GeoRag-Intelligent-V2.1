<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — extend silver.collars with the drillhole fields.
 *
 * 7 new columns, all additive. The hard constraint from the prompt:
 * DO NOT drop or rename any existing column on silver.collars (the
 * spatial pipeline depends on collar_id / hole_id / geom / etc.).
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.collars
              ADD COLUMN IF NOT EXISTS campaign_id       uuid REFERENCES silver.campaigns(id),
              ADD COLUMN IF NOT EXISTS drill_type        text DEFAULT 'DDH',
              ADD COLUMN IF NOT EXISTS purpose           text,
              ADD COLUMN IF NOT EXISTS hole_status       text DEFAULT 'completed',
              ADD COLUMN IF NOT EXISTS driller           text,
              ADD COLUMN IF NOT EXISTS geologist         text,
              ADD COLUMN IF NOT EXISTS bronze_source_id  uuid REFERENCES bronze.raw_collar_entries(id)
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS silver_collars_campaign_idx ON silver.collars (campaign_id) WHERE campaign_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS silver_collars_drill_type_idx ON silver.collars (drill_type)');
        DB::statement('CREATE INDEX IF NOT EXISTS silver_collars_hole_status_idx ON silver.collars (hole_status)');

        // Existing project_id column is already present (NOT NULL).
        // hole_type column already exists (DDH/RC/RAB). drill_type is a
        // separate but overlapping concept per the spec — we keep both
        // so the spec field is populated while existing readers of
        // hole_type continue to work.
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.collars.drill_type IS
              'Drilling method (DDH/RC/AC/RAB) per the §35.1 drillhole spec. Separate from `hole_type` (legacy column) which carries the same concept but with different historical values. New ingest writes both; consumers prefer drill_type.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.collars.bronze_source_id IS
              'Provenance link to bronze.raw_collar_entries. Set by the Dagster bronze→silver collar transform. NULL means the row predates the bronze-aware ingest path.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.silver_collars_hole_status_idx');
        DB::statement('DROP INDEX IF EXISTS silver.silver_collars_drill_type_idx');
        DB::statement('DROP INDEX IF EXISTS silver.silver_collars_campaign_idx');

        DB::statement(<<<'SQL'
            ALTER TABLE silver.collars
              DROP COLUMN IF EXISTS bronze_source_id,
              DROP COLUMN IF EXISTS geologist,
              DROP COLUMN IF EXISTS driller,
              DROP COLUMN IF EXISTS hole_status,
              DROP COLUMN IF EXISTS purpose,
              DROP COLUMN IF EXISTS drill_type,
              DROP COLUMN IF EXISTS campaign_id
        SQL);
    }
};
