<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * ON DELETE CASCADE for the thirteen FKs to silver.collars that had none
 * (§04e, database audit 2026-09-29 PG-2).
 *
 * The typed drill tables (2026_05_20_060300 assays_v2 + lithology,
 * 2026_05_20_060400 the seven geological singulars, 2026_05_20_060500
 * sample_intervals, 2026_05_20_060700 the three gold drillhole tables)
 * declared `REFERENCES silver.collars (collar_id)` with no ON DELETE clause,
 * so NO ACTION. silver.projects -> silver.collars is CASCADE, so deleting a
 * project cascaded into collars and then died on the first typed row:
 *
 *   ERROR: update or delete on table "collars" violates foreign key
 *          constraint "alteration_collar_id_fkey"
 *
 * ingest_tabular writes assays_v2 for every sample upload and #303 added
 * alteration / mineralization / structure, so any project with a drill-table
 * upload could no longer be deleted (ProjectController::destroy -> 500), and
 * neither could a single collar (CollarController::destroy). The fix matches
 * what samples, lithology_logs, surveys, geochemistry, well_log_curves,
 * drill_traces and the two gold *_visual tables already do: rows that
 * describe a hole go with the hole.
 *
 * ── Locking ─────────────────────────────────────────────────────────────
 * Each FK is swapped in its own short transaction: DROP CONSTRAINT and
 * ADD CONSTRAINT ... NOT VALID together, so there is never a moment with no
 * FK at all, and NOT VALID means the ADD does not scan the table while it
 * holds the ALTER lock. VALIDATE CONSTRAINT then runs as a separate
 * statement, which takes only SHARE UPDATE EXCLUSIVE on the referencing
 * table and ROW SHARE on collars — reads and writes carry on. Hence
 * $withinTransaction = false: one migration-wide transaction would hold
 * every ALTER lock until the last VALIDATE finished, which is exactly what
 * NOT VALID is meant to avoid.
 *
 * lock_timeout bounds how long each swap waits behind a long ingest
 * transaction; if it trips, the migration fails cleanly and a re-run is
 * safe — a constraint already at CASCADE (and validated) is skipped, and
 * one left NOT VALID by an interrupted run is validated.
 *
 * silver.evidence_items.passage_id ON DELETE RESTRICT (the audit's "also
 * latent" note) is deliberately NOT touched: RESTRICT vs SET NULL there is
 * an evidence-retention decision, not a defect fix.
 */
return new class extends Migration
{
    public $withinTransaction = false;

    /** @var list<array{0: string, 1: string}> [qualified table, constraint name] */
    private const FKS = [
        ['silver.assays_v2', 'assays_v2_collar_id_fkey'],
        ['silver.lithology', 'lithology_collar_id_fkey'],
        ['silver.structure', 'structure_collar_id_fkey'],
        ['silver.alteration', 'alteration_collar_id_fkey'],
        ['silver.mineralization', 'mineralization_collar_id_fkey'],
        ['silver.recovery', 'recovery_collar_id_fkey'],
        ['silver.specific_gravity', 'specific_gravity_collar_id_fkey'],
        ['silver.geotechnical', 'geotechnical_collar_id_fkey'],
        ['silver.downhole_geophysics', 'downhole_geophysics_collar_id_fkey'],
        ['silver.sample_intervals', 'sample_intervals_collar_id_fkey'],
        ['gold.assay_composites', 'assay_composites_collar_id_fkey'],
        ['gold.significant_intersections', 'significant_intersections_collar_id_fkey'],
        ['gold.drill_summaries', 'drill_summaries_collar_id_fkey'],
    ];

    private const LOCK_TIMEOUT = '15s';

    public function up(): void
    {
        $this->repoint('CASCADE', 'c');
    }

    public function down(): void
    {
        $this->repoint('NO ACTION', 'a');
    }

    /**
     * @param string $action ON DELETE action to install
     * @param string $code pg_constraint.confdeltype for that action
     */
    private function repoint(string $action, string $code): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::FKS as [$table, $constraint]) {
            $current = $this->constraint($table, $constraint);

            if ($current === null) {
                // Table or constraint absent (a cluster built before the
                // typed tables existed) — nothing to repoint.
                continue;
            }

            if ($current->confdeltype !== $code) {
                DB::transaction(function () use ($table, $constraint, $action): void {
                    DB::statement("SET LOCAL lock_timeout = '".self::LOCK_TIMEOUT."'");
                    DB::statement(
                        "ALTER TABLE {$table}
                            DROP CONSTRAINT {$constraint},
                            ADD CONSTRAINT {$constraint}
                                FOREIGN KEY (collar_id) REFERENCES silver.collars (collar_id)
                                ON DELETE {$action} NOT VALID",
                    );
                });
            }

            $after = $this->constraint($table, $constraint);
            if ($after !== null && ! $after->convalidated) {
                DB::statement("ALTER TABLE {$table} VALIDATE CONSTRAINT {$constraint}");
            }
        }
    }

    private function constraint(string $table, string $constraint): ?object
    {
        return DB::selectOne(
            'SELECT confdeltype, convalidated
               FROM pg_constraint
              WHERE conrelid = to_regclass(?) AND conname = ? AND contype = ?',
            [$table, $constraint, 'f'],
        );
    }
};
