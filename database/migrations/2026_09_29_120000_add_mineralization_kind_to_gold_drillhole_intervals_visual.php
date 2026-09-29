<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * gold.drillhole_intervals_visual gains a 'mineralization' interval_kind and
 * the mineralization_payload column that carries it (§04e).
 *
 * WHY
 *   silver.mineralization (one row per mineral per interval: mineral,
 *   abundance_pct, form, grain_size, notes) had no gold row, because the
 *   table's CHECK admitted lithology / alteration / structure /
 *   assay_high_grade / sample_window / other and none of them is a mineral
 *   occurrence. Bending 'other', or alteration_payload, to carry it would
 *   have been a guess about what the schema intends, so PR #303 left it out
 *   and the strip log read silver directly.
 *
 *   SME-approved 2026-09-29 (Kyle): promote it like alteration. One gold row
 *   per (collar, depth_from, depth_to); every mineral logged over the
 *   interval travels in
 *     mineralization_payload = {"minerals": [{"mineral", "abundance_pct",
 *                                             "form", "grain_size", "notes"}]}
 *   because the table's unique key is (collar_id, depth_from, depth_to,
 *   interval_kind) and two minerals over one interval must share a row.
 *   promote_silver_to_gold writes them.
 *
 * SAFETY
 *   Both statements widen: the new CHECK admits every value the old one did,
 *   so no existing row can violate it; the column is NOT NULL with a
 *   constant default, so existing rows read '{}' without a rewrite the
 *   application has to know about. The table is owned by the migration role
 *   (created by 2026_05_13_080000), which is what ALTER TABLE needs under the
 *   production-privileges CI job.
 *
 * REVERSIBILITY
 *   down() deletes the mineralization rows first (the restored CHECK would
 *   refuse them), then drops the column, then restores the six-value CHECK.
 *   Those rows are derived, not source: promote_silver_to_gold rebuilds them
 *   from silver.mineralization.
 */
return new class extends Migration
{
    private const CONSTRAINT = 'drillhole_intervals_visual_kind_valid';

    private const KINDS_BEFORE = "'lithology', 'alteration', 'structure', 'assay_high_grade', 'sample_window', 'other'";

    private const KINDS_AFTER = "'lithology', 'alteration', 'structure', 'assay_high_grade', 'sample_window', 'other', 'mineralization'";

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return; // the gold schema exists only on Postgres
        }

        $this->replaceKindCheck(self::KINDS_AFTER);

        DB::statement(<<<'SQL'
            ALTER TABLE gold.drillhole_intervals_visual
                ADD COLUMN IF NOT EXISTS mineralization_payload JSONB NOT NULL DEFAULT '{}'::jsonb
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN gold.drillhole_intervals_visual.mineralization_payload IS
              'interval_kind = mineralization only: {"minerals": [{"mineral", "abundance_pct", "form", "grain_size", "notes"}, ...]}, ordered by silver created_at, id. Rebuilt from silver.mineralization by promote_silver_to_gold. ''{}'' on every other kind.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement("DELETE FROM gold.drillhole_intervals_visual WHERE interval_kind = 'mineralization'");

        DB::statement('ALTER TABLE gold.drillhole_intervals_visual DROP COLUMN IF EXISTS mineralization_payload');

        $this->replaceKindCheck(self::KINDS_BEFORE);
    }

    private function replaceKindCheck(string $quotedKinds): void
    {
        DB::statement('ALTER TABLE gold.drillhole_intervals_visual DROP CONSTRAINT IF EXISTS '.self::CONSTRAINT);
        DB::statement(
            'ALTER TABLE gold.drillhole_intervals_visual ADD CONSTRAINT '.self::CONSTRAINT
            .' CHECK (interval_kind IN ('.$quotedKinds.'))',
        );
    }
};
