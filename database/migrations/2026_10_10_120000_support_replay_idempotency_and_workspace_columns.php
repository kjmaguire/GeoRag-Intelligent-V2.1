<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * ops.support_replay_runs gets an idempotency key, and both support tables
 * get the workspace_id column the support workflow now writes (2026-10 Hatchet
 * audit, findings 7 and 22).
 *
 * 1. replay_request_id uuid (UNIQUE). Laravel mints a replay_request_id for
 *    every "replay this ticket" click and sends it with the trigger. The
 *    column lets the workflow take it as its idempotency key: a duplicate
 *    dispatch finds the row the first one wrote and returns that, instead of
 *    running the support-agent chain a second time. It also lets the failure
 *    hook find the row a run left 'running' (the run's own replay_id is
 *    generated inside the task body and is not in the workflow input).
 *    NULLs are distinct in a unique index, so rows from before this migration
 *    need no backfill.
 *
 * 2. workspace_id on ops.support_replay_runs and ops.support_ticket_traces.
 *    database/raw/phase0/98-rls-tenant-isolation-block3.sql adds these (NOT
 *    NULL, FK, FORCE RLS with a strict tenant policy) after `migrate` on every
 *    cluster CD builds. The Python writers never named the column, so under
 *    that layer every INSERT into either table failed NOT NULL -- the replay
 *    row, and the trace link root_cause_investigation writes -- and a migrate-
 *    only database (CI) had no column to write. Adding it here, nullable and
 *    backfilled from the ticket exactly as block3 does, gives both shapes the
 *    column; block3's ADD COLUMN IF NOT EXISTS / SET NOT NULL / FK-by-name then
 *    apply on top unchanged (the FK names below are block3's, so it does not
 *    add a second one). The policies stay in block3: this migration creates
 *    none, so the FORCE-RLS parity baseline is untouched.
 *
 * down() drops replay_request_id and its index. It leaves the workspace_id
 * columns: the raw layer owns them (NOT NULL, FK, the policy expression), and
 * dropping a column a policy references would drop the policy with it.
 */
return new class extends Migration
{
    private const PLATFORM_WORKSPACE = 'a0000000-0000-0000-0000-000000000001';

    public function up(): void
    {
        if (! $this->present('ops.support_replay_runs') || ! $this->present('ops.support_ticket_traces')) {
            return;
        }

        DB::statement('ALTER TABLE ops.support_replay_runs ADD COLUMN IF NOT EXISTS replay_request_id uuid NULL');
        DB::statement(
            'CREATE UNIQUE INDEX IF NOT EXISTS support_replay_runs_request_id_uq
                 ON ops.support_replay_runs (replay_request_id)',
        );
        DB::statement(<<<'SQL'
COMMENT ON COLUMN ops.support_replay_runs.replay_request_id IS
    'Idempotency key minted by Laravel per replay request (support_replay workflow input). A duplicate dispatch returns the existing row instead of re-running the chain.'
SQL);

        foreach (['support_replay_runs', 'support_ticket_traces'] as $table) {
            DB::statement("ALTER TABLE ops.{$table} ADD COLUMN IF NOT EXISTS workspace_id uuid NULL");

            // The same backfill block3 runs, so a row from before the column
            // gets its ticket's workspace and the NOT NULL that follows holds.
            DB::statement(
                "UPDATE ops.{$table} x
                    SET workspace_id = t.workspace_id
                   FROM ops.support_tickets t
                  WHERE t.ticket_id = x.ticket_id
                    AND x.workspace_id IS NULL",
            );
            DB::statement(
                "UPDATE ops.{$table}
                    SET workspace_id = '".self::PLATFORM_WORKSPACE."'::uuid
                  WHERE workspace_id IS NULL",
            );

            DB::unprepared(<<<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE table_schema = 'ops' AND table_name = '{$table}'
           AND constraint_name = '{$table}_workspace_id_fkey'
    ) THEN
        ALTER TABLE ops.{$table}
            ADD CONSTRAINT {$table}_workspace_id_fkey
            FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id)
            ON DELETE CASCADE;
    END IF;
END \$\$;
SQL);
            DB::statement(
                "CREATE INDEX IF NOT EXISTS idx_{$table}_workspace_id ON ops.{$table} (workspace_id)",
            );
        }
    }

    public function down(): void
    {
        if (! $this->present('ops.support_replay_runs')) {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS ops.support_replay_runs_request_id_uq');
        DB::statement('ALTER TABLE ops.support_replay_runs DROP COLUMN IF EXISTS replay_request_id');
    }

    private function present(string $qualified): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return false;
        }

        return (bool) (DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            [$qualified],
        )->present ?? false);
    }
};
