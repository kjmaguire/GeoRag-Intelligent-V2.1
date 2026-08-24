<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Give every table that holds project content a cascading foreign key, so
 * deleting a project actually removes it.
 *
 * ## What was measured
 *
 * A census of production on 2026-08-24 found 41 tables carrying a
 * `project_id` column:
 *
 *   - 13 already CASCADE from `silver.projects` — correct, untouched here.
 *   -  3 are NO ACTION and BLOCK the delete  — fixed by 2026_08_24_060000.
 *   -  6 are SET NULL, so the row survives with a null pointer.
 *   - 17 have NO FOREIGN KEY AT ALL, so neither the cascade nor
 *     `ProjectController::destroy`'s hand-maintained list can reach them.
 *
 * The last two groups are what this migration closes.
 *
 * ## SET NULL -> CASCADE, and why only these five
 *
 * A SET NULL row is not deleted, it is orphaned: afterwards it is
 * indistinguishable from a record that legitimately never had a project.
 * The five converted here are the ones `ProjectController::destroy` ALREADY
 * deletes explicitly by project_id, so their intent is not in question — the
 * cascade just makes the hand-list entry redundant, which is the point.
 *
 * `silver.geochronology_samples` is deliberately NOT converted: nothing
 * deletes it today, so switching it to CASCADE would be a new behaviour
 * decision rather than the enforcement of an existing one. It holds zero rows;
 * it is recorded in ProjectDeleteCoverageTest as a decision still owed.
 *
 * ## audit.query_audit_log is deliberately excluded
 *
 * An audit log a user can erase by deleting the project is not an audit log.
 * It keeps time-based retention instead (retention_sweep.py,
 * QUERY_AUDIT_RETENTION_DAYS, default 180). That exemption is recorded in
 * ProjectDeleteCoverageTest::SURVIVES_DELETE so it stays a choice rather than
 * an oversight.
 *
 * ## Pre-existing orphans
 *
 * A foreign key cannot be added while violating rows exist. Every table below
 * held zero rows in production except `chat_conversations` (4 rows, pointing
 * at a project deleted earlier that day). Rows whose project_id references a
 * project that no longer exists are deleted first — they are unreachable by
 * definition, which is the whole defect being fixed.
 */
return new class extends Migration
{
    /**
     * Tables gaining `project_id -> silver.projects ON DELETE CASCADE`.
     *
     * @var list<string>
     */
    private const NEEDS_FK = [
        'gold.cross_section_panels',
        'gold.drillhole_intervals_visual',
        'gold.structure_measurements_visual',
        'interpretation.interpretation_comments',
        'interpretation.interpretation_notes',
        'interpretation.interpretation_section_lines',
        'interpretation.interpretation_target_zones',
        'public.chat_conversations',
        'silver.archive_ingest_runs',
        'silver.assay_samples',
        'silver.collab_anchors',
        'silver.completeness_findings',
        'silver.data_quality_flags',
        'silver.document_versions',
        'silver.query_traces',
        'silver.reports',
        'targeting.target_recommendations',
    ];

    /**
     * Tables whose existing SET NULL relation becomes CASCADE.
     *
     * @var list<string>
     */
    private const SET_NULL_TO_CASCADE = [
        'silver.answer_runs',
        'silver.geophysics_surveys',
        'silver.raster_layers',
        'silver.seismic_surveys',
        'silver.spatial_features',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::NEEDS_FK as $table) {
            if (! $this->exists($table) || ! $this->owned($table)) {
                continue;
            }

            // Unreachable rows: a project_id pointing at a project that is gone.
            DB::statement(
                "DELETE FROM {$table} WHERE project_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM silver.projects p WHERE p.project_id = {$table}.project_id)",
            );

            $constraint = $this->constraintName($table);

            DB::statement(
                "ALTER TABLE {$table} ADD CONSTRAINT {$constraint}
                   FOREIGN KEY (project_id) REFERENCES silver.projects(project_id) ON DELETE CASCADE",
            );
        }

        foreach (self::SET_NULL_TO_CASCADE as $table) {
            if (! $this->exists($table) || ! $this->owned($table)) {
                continue;
            }

            /** @var list<object{conname: string}> $existing */
            $existing = DB::select(
                "SELECT con.conname FROM pg_constraint con
                  WHERE con.conrelid = ?::regclass AND con.contype = 'f'
                    AND con.confrelid = 'silver.projects'::regclass
                    AND con.confdeltype = 'n'",
                [$table],
            );

            foreach ($existing as $row) {
                DB::statement("ALTER TABLE {$table} DROP CONSTRAINT \"{$row->conname}\"");
                DB::statement(
                    "ALTER TABLE {$table} ADD CONSTRAINT \"{$row->conname}\"
                       FOREIGN KEY (project_id) REFERENCES silver.projects(project_id) ON DELETE CASCADE",
                );
            }
        }
    }

    public function down(): void
    {
        // Not reversible on purpose: restoring it would restore "deleting a
        // project leaves its content behind", which is the defect. Drop a
        // single constraint explicitly in a new migration if one relationship
        // genuinely needs different semantics, and say why.
    }

    /**
     * A table listed here may not exist in every environment — the
     * interpretation.* and silver.assay_samples tables come from
     * database/raw/phase0 bootstrap SQL that was never applied to Azure.
     * Skipping is a no-op, not a failure (the lesson of the 2026-08-17
     * silver.mineral_claims outage, which took project deletion down entirely).
     */
    private function exists(string $qualified): bool
    {
        [$schema, $table] = explode('.', $qualified, 2);

        return DB::selectOne(
            'SELECT 1 AS ok FROM information_schema.columns
              WHERE table_schema = ? AND table_name = ? AND column_name = ?',
            [$schema, $table, 'project_id'],
        ) !== null;
    }

    /**
     * ALTER TABLE needs ownership. CI's migrations-production-privileges gate
     * deliberately splits ownership between the role applying database/raw/
     * and the role running migrations; without this check the whole chain
     * aborts there. Mirrors 2026_08_24_010000.
     */
    private function owned(string $qualified): bool
    {
        return DB::selectOne(
            "SELECT 1 AS ok FROM pg_class c
              WHERE c.oid = ?::regclass AND pg_has_role(current_user, c.relowner, 'USAGE')",
            [$qualified],
        ) !== null;
    }

    private function constraintName(string $qualified): string
    {
        [, $table] = explode('.', $qualified, 2);

        return "{$table}_project_id_fkey";
    }
};
