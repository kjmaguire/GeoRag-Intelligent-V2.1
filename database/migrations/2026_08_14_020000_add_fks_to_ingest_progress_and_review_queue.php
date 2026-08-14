<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * 2026-08-14 DB audit item M3 — referential integrity on the two hot
 * operational tables that shipped without any FKs:
 *
 *   silver.ingest_progress (2026_05_24_230000)
 *     workspace_id → silver.workspaces(workspace_id)  ON DELETE CASCADE
 *     project_id   → silver.projects(project_id)      ON DELETE CASCADE
 *                    (nullable since 2026_05_25_041533)
 *     report_id    → silver.reports(report_id)        ON DELETE SET NULL
 *
 *   silver.review_queue (2026_05_24_120000)
 *     workspace_id        → silver.workspaces(workspace_id) ON DELETE CASCADE
 *     project_id          → silver.projects(project_id)     ON DELETE CASCADE
 *     assigned_to_user_id → public.users(id)                ON DELETE SET NULL
 *                           (bigint on both sides — review_queue deliberately
 *                           chose bigint to match Laravel users.id)
 *
 * All referenced PKs are uuid except users.id (bigint); column types were
 * verified against the creating migrations before writing this one.
 *
 * Lock strategy: each FK is added NOT VALID (only a brief metadata lock,
 * no full-table scan under ACCESS EXCLUSIVE), then VALIDATE CONSTRAINT
 * runs separately — validation takes only SHARE UPDATE EXCLUSIVE, so
 * concurrent ingest writes are not blocked for the duration of the scan.
 *
 * Orphan handling — VALIDATE would fail if historical rows reference
 * deleted parents, so BEFORE validating we reconcile existing data to the
 * semantics each FK will enforce going forward:
 *   - rows whose workspace/project no longer exists are DELETEd (matches
 *     the ON DELETE CASCADE the FK would have applied);
 *   - dangling report_id / assigned_to_user_id references are set NULL
 *     (matches ON DELETE SET NULL). review_queue deletes cascade into
 *     silver.review_audit_log via its existing queue_id FK.
 *
 * Idempotency: every ADD CONSTRAINT is guarded by a pg_constraint
 * existence check; VALIDATE on an already-valid constraint is a no-op.
 *
 * Test-DB parity: no *_provision_*_for_test_db sibling is needed — every
 * table touched here (including public.users) is created by the ordinary
 * migration chain, which the pgsql test DB runs in full. Skipped on
 * sqlite, where silver.* never exists.
 */
return new class extends Migration
{
    /** @var array<string, array{table: string, column: string, refs: string, action: string}> */
    private const FOREIGN_KEYS = [
        'ingest_progress_workspace_id_fk' => [
            'table' => 'silver.ingest_progress',
            'column' => 'workspace_id',
            'refs' => 'silver.workspaces (workspace_id)',
            'action' => 'ON DELETE CASCADE',
        ],
        'ingest_progress_project_id_fk' => [
            'table' => 'silver.ingest_progress',
            'column' => 'project_id',
            'refs' => 'silver.projects (project_id)',
            'action' => 'ON DELETE CASCADE',
        ],
        'ingest_progress_report_id_fk' => [
            'table' => 'silver.ingest_progress',
            'column' => 'report_id',
            'refs' => 'silver.reports (report_id)',
            'action' => 'ON DELETE SET NULL',
        ],
        'review_queue_workspace_id_fk' => [
            'table' => 'silver.review_queue',
            'column' => 'workspace_id',
            'refs' => 'silver.workspaces (workspace_id)',
            'action' => 'ON DELETE CASCADE',
        ],
        'review_queue_project_id_fk' => [
            'table' => 'silver.review_queue',
            'column' => 'project_id',
            'refs' => 'silver.projects (project_id)',
            'action' => 'ON DELETE CASCADE',
        ],
        'review_queue_assigned_to_user_fk' => [
            'table' => 'silver.review_queue',
            'column' => 'assigned_to_user_id',
            'refs' => 'public.users (id)',
            'action' => 'ON DELETE SET NULL',
        ],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // ── 1. Reconcile orphans so VALIDATE cannot fail ─────────────────
        // CASCADE-semantics columns: drop rows whose parent is gone.
        DB::statement(<<<'SQL'
            DELETE FROM silver.ingest_progress p
            WHERE NOT EXISTS (
                SELECT 1 FROM silver.workspaces w
                WHERE w.workspace_id = p.workspace_id
            )
        SQL);
        DB::statement(<<<'SQL'
            DELETE FROM silver.ingest_progress p
            WHERE p.project_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM silver.projects pr
                WHERE pr.project_id = p.project_id
            )
        SQL);
        DB::statement(<<<'SQL'
            DELETE FROM silver.review_queue q
            WHERE NOT EXISTS (
                SELECT 1 FROM silver.workspaces w
                WHERE w.workspace_id = q.workspace_id
            )
        SQL);
        DB::statement(<<<'SQL'
            DELETE FROM silver.review_queue q
            WHERE NOT EXISTS (
                SELECT 1 FROM silver.projects pr
                WHERE pr.project_id = q.project_id
            )
        SQL);

        // SET NULL-semantics columns: null out dangling references.
        DB::statement(<<<'SQL'
            UPDATE silver.ingest_progress p
            SET report_id = NULL
            WHERE p.report_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM silver.reports r
                WHERE r.report_id = p.report_id
            )
        SQL);
        DB::statement(<<<'SQL'
            UPDATE silver.review_queue q
            SET assigned_to_user_id = NULL
            WHERE q.assigned_to_user_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM public.users u
                WHERE u.id = q.assigned_to_user_id
            )
        SQL);

        // ── 2. Add each FK NOT VALID (guarded), then VALIDATE ────────────
        foreach (self::FOREIGN_KEYS as $name => $fk) {
            DB::statement(<<<SQL
                DO \$\$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = '{$name}'
                          AND conrelid = '{$fk['table']}'::regclass
                    ) THEN
                        ALTER TABLE {$fk['table']}
                            ADD CONSTRAINT {$name}
                            FOREIGN KEY ({$fk['column']})
                            REFERENCES {$fk['refs']}
                            {$fk['action']}
                            NOT VALID;
                    END IF;
                END\$\$;
            SQL);

            DB::statement("ALTER TABLE {$fk['table']} VALIDATE CONSTRAINT {$name}");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::FOREIGN_KEYS as $name => $fk) {
            DB::statement("ALTER TABLE {$fk['table']} DROP CONSTRAINT IF EXISTS {$name}");
        }
    }
};
