<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * FORCE ROW LEVEL SECURITY on the tables the reconciliation migrations enabled.
 *
 * `ENABLE ROW LEVEL SECURITY` does not apply to the table's OWNER. Postgres
 * exempts the owner unless you also say `FORCE`. The raw SQL layer gets this
 * right — every table there is `ENABLE` immediately followed by `FORCE` — but
 * the migration chain that replaced it does not:
 * `2026_05_25_175214::reconcileTable()` issues `ENABLE` and stops, and so does
 * `installCanonicalPolicy()` in `2026_05_25_180924`.
 *
 * On Azure the owner is the `georag` role, which is what `MIGRATE_DB_USERNAME`
 * connects as. The day-to-day application path is unaffected — it connects as
 * `georag_app`, which is not the owner and is therefore subject to the
 * policies. What silently ignored tenancy was every migration, every `artisan`
 * command and every ad-hoc `psql -U georag` session, on tables including
 * silver.projects, silver.reports and audit.audit_ledger.
 *
 * The docblock on `2026_08_19_050000` asserts "every one of these tables is
 * under FORCE ROW LEVEL SECURITY" and uses that as the justification for
 * widening grants. For the reconciled tables that assertion was not true. It
 * is now.
 *
 * Idempotent: FORCE is a table attribute, and setting it twice is a no-op.
 * Tables absent from this cluster are skipped rather than failing the deploy —
 * several of them are created by `database/raw/`, which CD has never applied
 * (see ops/runbooks/raw-sql-layer.md).
 */
return new class extends Migration
{
    /**
     * Matches TARGETS in 2026_05_25_175214 plus the canonical-policy set.
     *
     * @var list<string>
     */
    private const TABLES = [
        'audit.audit_ledger',
        'audit.audit_ledger_verification_runs',
        'gold.cross_section_panels',
        'gold.drillhole_intervals_visual',
        'gold.structure_measurements_visual',
        'silver.geochemistry',
        'silver.geological_formations',
        'silver.geophysics_surveys',
        'silver.historic_workings',
        'silver.project_boundaries',
        'silver.projects',
        'silver.reports',
        'silver.review_queue',
        'silver.spatial_features',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;  // RLS is a Postgres feature
        }

        foreach (self::TABLES as $qualified) {
            if (! $this->tableExists($qualified)) {
                continue;
            }

            DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::TABLES as $qualified) {
            if ($this->tableExists($qualified)) {
                DB::statement("ALTER TABLE {$qualified} NO FORCE ROW LEVEL SECURITY");
            }
        }
    }

    /**
     * `to_regclass` returns NULL rather than raising for an absent relation,
     * so this is one round trip and no exception handling.
     */
    private function tableExists(string $qualified): bool
    {
        return DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            [$qualified],
        )?->present ?? false;
    }
};
