<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Brings five workspace-scoped silver tables under version control.
 *
 * WHAT WAS WRONG
 * --------------
 * silver.exports, silver.surveys, silver.seismic_surveys,
 * silver.structured_record_lineage and silver.well_log_curves all carry a
 * NOT NULL `workspace_id` and a `<table>_workspace_isolation` RLS policy in
 * the dev database. Only one of the five — well_log_curves, via
 * database/raw/phase0/96-rls-tenant-isolation-block1.sql — is created by
 * anything in this repository. For the other four, neither the column nor
 * the policy appears in any migration or raw SQL file; they were applied out
 * of band and have never been in version control.
 *
 * Two concrete consequences, both observed rather than theorised:
 *
 *   1. A database built purely from migrations has these tables with NO
 *      workspace_id and NO row-level security. That is what georag_test
 *      looks like today, and it is what a freshly provisioned cluster would
 *      look like. silver.exports is the one that stings: it holds users'
 *      generated data extracts, including minio_path and download_url.
 *
 *   2. tests/Feature/Tenancy/WorkspaceRlsCoverageTest — the guard that
 *      exists precisely to catch "a migration added workspace_id and forgot
 *      ENABLE ROW LEVEL SECURITY" — cannot see any of them. It asserts
 *      "table HAS workspace_id => table MUST have RLS", so a table missing
 *      the column is not a failure, it is invisible. The five tables that
 *      most need the guard are the five it skips.
 *
 * NO-OP WHEN COVERED
 * ------------------
 * Same semantics as
 * 2026_05_25_175214_enable_rls_on_phase0_workspace_tables_reconciliation:
 * for each table we install only what is actually absent. Where the column,
 * the FK, the index, RLS and a policy already exist — dev, and every
 * environment where the out-of-band SQL was applied — this migration
 * changes nothing at all. Where they are absent it is a first-time install.
 *
 * The policy body is copied from what the dev database actually has rather
 * than from what would be ideal. That shape is fail-open: with
 * `app.workspace_id` unset the NULLIF branch matches every row. That is a
 * known, deliberately parked decision covering roughly a dozen policies (see
 * memory/project_parked_items_2026_05_25.md) and tightening it is a separate
 * change with its own blast radius. Reconciling to a DIFFERENT shape here
 * would silently change production behaviour under cover of a parity fix,
 * which is exactly the sort of thing this migration exists to stop. Parity
 * now; the fail-open question stays open.
 *
 * `workspace_id` is added NULL-able even though dev has it NOT NULL. On a
 * first-time install the backfill below cannot be guaranteed to reach every
 * row (an export whose project was deleted, a survey whose collar predates
 * tenancy), and a failed SET NOT NULL would abort the whole deploy. A row
 * left NULL is invisible under the equality branch of the policy, which
 * fails safe.
 */
return new class extends Migration
{
    /**
     * table => backfill statement resolving workspace_id from the parent row.
     *
     * @var array<string, string>
     */
    private const TARGETS = [
        'exports' => 'UPDATE silver.exports t SET workspace_id = p.workspace_id
                        FROM silver.projects p
                       WHERE p.project_id = t.project_id AND t.workspace_id IS NULL',
        'surveys' => 'UPDATE silver.surveys t SET workspace_id = c.workspace_id
                        FROM silver.collars c
                       WHERE c.collar_id = t.collar_id AND t.workspace_id IS NULL',
        'seismic_surveys' => 'UPDATE silver.seismic_surveys t SET workspace_id = p.workspace_id
                        FROM silver.projects p
                       WHERE p.project_id = t.project_id AND t.workspace_id IS NULL',
        'structured_record_lineage' => 'UPDATE silver.structured_record_lineage t SET workspace_id = e.workspace_id
                        FROM silver.evidence_items e
                       WHERE e.evidence_id = t.evidence_id AND t.workspace_id IS NULL',
        'well_log_curves' => 'UPDATE silver.well_log_curves t SET workspace_id = c.workspace_id
                        FROM silver.collars c
                       WHERE c.collar_id = t.collar_id AND t.workspace_id IS NULL',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            // SQLite has no RLS; the fast suite gets its columns from the
            // dedicated *_for_test_db provisioning migrations.
            return;
        }

        foreach (self::TARGETS as $table => $backfill) {
            if (! $this->tableExists($table)) {
                continue;
            }

            $addedColumn = false;
            if (! $this->columnExists($table, 'workspace_id')) {
                DB::statement("ALTER TABLE silver.{$table} ADD COLUMN workspace_id uuid");
                $addedColumn = true;
            }

            if ($addedColumn) {
                DB::statement($backfill);
            }

            DB::statement(
                "CREATE INDEX IF NOT EXISTS idx_{$table}_workspace_id
                    ON silver.{$table} (workspace_id)",
            );

            $this->addWorkspaceFkIfMissing($table);

            // ENABLE/FORCE are idempotent; re-issuing them where they are
            // already set is a no-op rather than an error.
            DB::statement("ALTER TABLE silver.{$table} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE silver.{$table} FORCE ROW LEVEL SECURITY");

            if ($this->policyCount($table) === 0) {
                DB::statement($this->canonicalPolicySql($table));
            }
        }
    }

    public function down(): void
    {
        // Deliberately irreversible. Dropping RLS or a tenancy column on
        // rollback would turn a rollback into a data-exposure event, and in
        // every environment that already had these out of band this
        // migration added nothing to remove.
    }

    private function canonicalPolicySql(string $table): string
    {
        $predicate = "NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                      OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid";

        return "CREATE POLICY {$table}_workspace_isolation ON silver.{$table}
                    USING ({$predicate})
                    WITH CHECK ({$predicate})";
    }

    private function tableExists(string $table): bool
    {
        return DB::selectOne(
            "SELECT 1 AS present FROM information_schema.tables
              WHERE table_schema = 'silver' AND table_name = ?",
            [$table],
        ) !== null;
    }

    private function columnExists(string $table, string $column): bool
    {
        return DB::selectOne(
            "SELECT 1 AS present FROM information_schema.columns
              WHERE table_schema = 'silver' AND table_name = ? AND column_name = ?",
            [$table, $column],
        ) !== null;
    }

    private function policyCount(string $table): int
    {
        $row = DB::selectOne(
            "SELECT COUNT(*) AS n FROM pg_policies
              WHERE schemaname = 'silver' AND tablename = ?",
            [$table],
        );

        return (int) ($row->n ?? 0);
    }

    private function addWorkspaceFkIfMissing(string $table): void
    {
        $constraint = "{$table}_workspace_id_fkey";

        $exists = DB::selectOne(
            "SELECT 1 AS present FROM information_schema.table_constraints
              WHERE table_schema = 'silver' AND table_name = ? AND constraint_name = ?",
            [$table, $constraint],
        );

        if ($exists !== null) {
            return;
        }

        DB::statement(
            "ALTER TABLE silver.{$table}
                ADD CONSTRAINT {$constraint}
                FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
                ON DELETE CASCADE",
        );
    }
};
