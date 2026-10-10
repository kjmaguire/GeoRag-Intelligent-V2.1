<?php

declare(strict_types=1);

namespace Tests\Concerns;

use Closure;
use Illuminate\Support\Facades\DB;
use Throwable;

/**
 * Shared harness for the audit-ledger tests (AuditQueryLogRetentionGrantTest,
 * AuditHashTriggerCreatedAtTest, AuditLedgerAppendOnlyTest).
 *
 * Everything runs in a transaction that is rolled back: the ledger is
 * append-only, so a committed probe row could never be cleaned up. The
 * `SET LOCAL ROLE georag_app` probes revert with that transaction. The suite
 * connects as the table OWNER (a superuser locally), which policies and
 * privileges do not bind, so the role switch is what makes these an honest test
 * of the AWS path.
 *
 * The using class must call startAuditProbe() from setUp() (after skipping on
 * non-Postgres drivers) and endAuditProbe() from tearDown().
 */
trait ProbesAuditLedger
{
    private const WS_A = '5a0d1f00-0000-4000-8000-0000000000c1';

    private const AUDIT_MIGRATIONS = [
        'grant_delete' => '2026_10_10_100000_grant_delete_on_query_audit_log_to_georag_app.php',
        'hash_trigger' => '2026_10_10_100100_audit_hash_trigger_stamps_created_at_after_chain_lock.php',
        'append_only' => '2026_10_10_100200_make_audit_ledger_append_only.php',
        'matrix' => '2026_08_19_050000_grant_georag_app_privileges_on_migrated_objects.php',
    ];

    private function startAuditProbe(): void
    {
        $ready = DB::selectOne(
            "SELECT to_regclass('audit.audit_ledger') IS NOT NULL
                AND to_regclass('audit.query_audit_log') IS NOT NULL
                AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') AS ready",
        )->ready;

        if (! $ready) {
            $this->markTestSkipped('audit.audit_ledger, audit.query_audit_log or georag_app is absent from this cluster.');
        }

        DB::beginTransaction();

        // A session-level binding left behind by an earlier test would make the
        // ledger's workspace policies reject the probe rows for the wrong reason.
        DB::statement("SELECT set_config('app.workspace_id', '', true)");
    }

    private function endAuditProbe(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            try {
                DB::statement('RESET ROLE');
            } catch (Throwable) {
                // The connection may already be gone.
            }

            if (DB::transactionLevel() > 0) {
                DB::rollBack();
            }
        }
    }

    /**
     * Insert as the connection's own role. `$createdAt` is passed to the
     * INSERT on purpose: the trigger is expected to overwrite it.
     *
     * @return array{id: string, previous_hash: ?string, hash: string}
     */
    private function insertRow(?string $workspace, string $action, ?string $createdAt = null): array
    {
        $row = DB::selectOne(
            <<<'SQL'
            INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload, created_at)
            VALUES (?::uuid, 'system', ?, '{"k": 1}'::jsonb, COALESCE(?::timestamptz, clock_timestamp()))
            RETURNING id::text AS id, encode(previous_hash, 'hex') AS previous_hash, encode(hash, 'hex') AS hash
            SQL,
            [$workspace, $action, $createdAt],
        );

        return (array) $row;
    }

    private function asAppRole(): void
    {
        $member = DB::selectOne("SELECT pg_has_role(current_user, 'georag_app', 'MEMBER') AS ok")->ok;
        if (! $member) {
            $this->markTestSkipped('The suite login cannot SET ROLE georag_app.');
        }

        DB::statement('SET LOCAL ROLE georag_app');
    }

    private function isSuperuser(): bool
    {
        return (bool) DB::selectOne('SELECT rolsuper FROM pg_roles WHERE rolname = current_user')->rolsuper;
    }

    private function hasPrivilege(string $role, string $table, string $privilege): bool
    {
        return (bool) DB::selectOne(
            'SELECT has_table_privilege(?, ?, ?) AS ok',
            [$role, $table, $privilege],
        )->ok;
    }

    /**
     * Run a statement that must fail, inside a savepoint so the surrounding
     * test transaction survives the error, and return the database's message.
     */
    private function failureOf(Closure $statement): string
    {
        try {
            DB::transaction($statement);
        } catch (Throwable $e) {
            return $e->getMessage();
        }

        $this->fail('The statement was expected to be refused by the database but succeeded.');
    }

    private function migration(string $key): object
    {
        return require database_path('migrations/'.self::AUDIT_MIGRATIONS[$key]);
    }
}
