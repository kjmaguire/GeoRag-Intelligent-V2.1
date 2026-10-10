<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use RuntimeException;
use Tests\Concerns\ProbesAuditLedger;
use Tests\TestCase;

/**
 * 2026_10_10_100200: audit.audit_ledger is append-only for the application role.
 *
 * `georag_app` held UPDATE on the ledger (the 2026_08_19_050000 matrix grants it
 * on the whole `audit` schema) and the only trigger was BEFORE INSERT, so the app
 * role could rewrite a row and re-hash history. Now UPDATE/DELETE are revoked from
 * `georag_app` and `georag_write`, and a BEFORE UPDATE OR DELETE trigger refuses
 * every role, the owner and superusers included.
 *
 * The case that is easy to get wrong is the pair "UPDATE is revoked" / "inserts
 * still work as the app role": revoking UPDATE while the hash trigger still ends in
 * SELECT ... FOR UPDATE makes every audit insert by georag_app fail, because a row
 * lock needs the UPDATE privilege (2026_10_10_100100 removes the lock; the revoke
 * migration refuses to run while it is there). Fails on the previous state of the
 * database; the matrix re-run case fails on the previous 2026_08_19_050000.
 */
final class AuditLedgerAppendOnlyTest extends TestCase
{
    use ProbesAuditLedger;

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('The audit schema and its triggers are Postgres-only.');
        }

        $this->startAuditProbe();
    }

    protected function tearDown(): void
    {
        $this->endAuditProbe();

        parent::tearDown();
    }

    #[Test]
    public function the_app_role_holds_insert_and_select_on_the_ledger_but_not_update_or_delete(): void
    {
        $this->assertTrue($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'INSERT'));
        $this->assertTrue($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'SELECT'));
        $this->assertFalse($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'UPDATE'), 'georag_app can rewrite and re-hash history');
        $this->assertFalse($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'DELETE'));

        $hasWrite = DB::selectOne("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_write') AS present")->present;
        if ($hasWrite) {
            $this->assertFalse($this->hasPrivilege('georag_write', 'audit.audit_ledger', 'UPDATE'));
            $this->assertFalse($this->hasPrivilege('georag_write', 'audit.audit_ledger', 'DELETE'));
        }
    }

    #[Test]
    public function the_verifier_keeps_update_on_its_own_table(): void
    {
        $this->assertTrue(
            $this->hasPrivilege('georag_app', 'audit.audit_ledger_verification_runs', 'UPDATE'),
            'audit.run_verification() UPDATEs the run row it just inserted; losing this breaks the nightly verify',
        );
    }

    #[Test]
    public function inserts_by_the_app_role_still_work_without_the_update_privilege(): void
    {
        // The trap: with UPDATE revoked and the old trigger body (SELECT ... FOR
        // UPDATE) this is "permission denied for table audit_ledger".
        $this->asAppRole();

        $row = DB::selectOne(
            "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload)
             VALUES (NULL, 'system', 'append_only.app_insert', '{}'::jsonb)
             RETURNING length(hash) AS hash_len",
        );
        $this->assertSame(32, (int) $row->hash_len);

        $second = DB::selectOne(
            "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload)
             VALUES (?::uuid, 'system', 'append_only.app_insert', '{}'::jsonb)
             RETURNING length(hash) AS hash_len",
            [self::WS_A],
        );
        $this->assertSame(32, (int) $second->hash_len, 'a workspace chain insert also works');
    }

    #[Test]
    public function the_trap_is_real_a_row_lock_in_the_hash_trigger_would_stop_every_audit_insert(): void
    {
        // Pins WHY 2026_10_10_100100 drops the lock: put the previous
        // definition back (SELECT ... FOR UPDATE) with UPDATE already revoked,
        // and the application role can no longer write a single audit row.
        $this->migration('hash_trigger')->down();
        $this->asAppRole();

        $message = $this->failureOf(fn () => DB::selectOne(
            "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload)
             VALUES (NULL, 'system', 'append_only.trap', '{}'::jsonb) RETURNING 1 AS one",
        ));

        $this->assertStringContainsString('permission denied for table audit_ledger', $message);
    }

    #[Test]
    public function the_app_role_is_refused_an_update_and_a_delete(): void
    {
        $row = $this->insertRow(self::WS_A, 'append_only.victim');
        $this->asAppRole();

        $update = $this->failureOf(fn () => DB::update(
            "UPDATE audit.audit_ledger SET payload = '{}'::jsonb WHERE id = ?::uuid",
            [$row['id']],
        ));
        $this->assertStringContainsString('permission denied', $update);

        $delete = $this->failureOf(fn () => DB::delete(
            'DELETE FROM audit.audit_ledger WHERE id = ?::uuid',
            [$row['id']],
        ));
        $this->assertStringContainsString('permission denied', $delete);
    }

    #[Test]
    public function the_trigger_refuses_update_and_delete_even_for_the_table_owner(): void
    {
        // The layer that survives a re-grant: no role-based exemption, so the
        // owner (and a superuser) are stopped too.
        $row = $this->insertRow(self::WS_A, 'append_only.owner');

        $update = $this->failureOf(fn () => DB::update(
            "UPDATE audit.audit_ledger SET hash = decode('00', 'hex') WHERE id = ?::uuid",
            [$row['id']],
        ));
        $this->assertStringContainsString('audit.audit_ledger is append-only', $update);

        $delete = $this->failureOf(fn () => DB::delete(
            'DELETE FROM audit.audit_ledger WHERE id = ?::uuid',
            [$row['id']],
        ));
        $this->assertStringContainsString('audit.audit_ledger is append-only', $delete);

        $this->assertSame(
            1,
            (int) DB::selectOne('SELECT count(*) AS n FROM audit.audit_ledger WHERE id = ?::uuid', [$row['id']])->n,
            'the row survived both attempts',
        );
    }

    #[Test]
    public function the_trigger_still_refuses_after_a_privilege_is_handed_back(): void
    {
        // What a re-run of database/raw/phase1/10-georag-app-role.sql, or a
        // future GRANT ALL, would do: the privilege returns, the write must not.
        $row = $this->insertRow(self::WS_A, 'append_only.regrant');
        DB::statement('GRANT UPDATE, DELETE ON audit.audit_ledger TO georag_app');
        $this->asAppRole();

        $update = $this->failureOf(fn () => DB::update(
            "UPDATE audit.audit_ledger SET payload = '{\"forged\": true}'::jsonb WHERE id = ?::uuid",
            [$row['id']],
        ));
        $this->assertStringContainsString('audit.audit_ledger is append-only', $update);

        $delete = $this->failureOf(fn () => DB::delete(
            'DELETE FROM audit.audit_ledger WHERE id = ?::uuid',
            [$row['id']],
        ));
        $this->assertStringContainsString('audit.audit_ledger is append-only', $delete);
    }

    #[Test]
    public function a_superuser_can_still_break_glass_with_the_replica_role(): void
    {
        if (! $this->isSuperuser()) {
            $this->markTestSkipped('session_replication_role needs a superuser; this login is not one.');
        }

        $row = $this->insertRow(self::WS_A, 'append_only.break_glass');
        DB::statement('SET LOCAL session_replication_role = replica');

        $this->assertSame(
            1,
            DB::delete('DELETE FROM audit.audit_ledger WHERE id = ?::uuid', [$row['id']]),
            'the documented break-glass for a reviewed prune',
        );
    }

    #[Test]
    public function re_running_the_privilege_matrix_does_not_hand_update_back(): void
    {
        // 2026_08_19_050000 grants INSERT, SELECT, UPDATE on every audit table.
        // Re-running it (migrate:refresh, a restored migrations table) used to
        // undo the revoke.
        $this->migration('matrix')->up();

        $this->assertFalse($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'UPDATE'));
        $this->assertFalse($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'DELETE'));
        $this->assertTrue($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'INSERT'));
        $this->assertTrue(
            $this->hasPrivilege('georag_app', 'audit.audit_ledger_verification_runs', 'UPDATE'),
            'the matrix still gives the other audit tables their UPDATE',
        );
    }

    #[Test]
    public function the_append_only_migration_is_reversible_and_repeatable(): void
    {
        $migration = $this->migration('append_only');

        $migration->up();
        $this->assertFalse($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'UPDATE'), 'up() twice is a no-op');

        $migration->down();
        $this->assertTrue($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'UPDATE'), 'down() gives the privilege back');
        $this->assertSame(
            0,
            (int) DB::selectOne(
                "SELECT count(*) AS n FROM pg_trigger WHERE tgrelid = 'audit.audit_ledger'::regclass AND tgname = 'audit_ledger_append_only_trg'",
            )->n,
            'down() removes the trigger',
        );

        $migration->up();
        $this->assertFalse($this->hasPrivilege('georag_app', 'audit.audit_ledger', 'UPDATE'));
        $this->assertSame(
            1,
            (int) DB::selectOne(
                "SELECT count(*) AS n FROM pg_trigger WHERE tgrelid = 'audit.audit_ledger'::regclass AND tgname = 'audit_ledger_append_only_trg' AND NOT tgisinternal",
            )->n,
        );
    }

    #[Test]
    public function the_revoke_refuses_to_run_while_the_hash_trigger_still_takes_a_row_lock(): void
    {
        // Out-of-order rollout, or a stale raw 90 re-applied by hand: the old
        // function body is back. Revoking UPDATE now would stop every audit
        // insert, so the migration must stop first.
        $this->migration('hash_trigger')->down();

        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('still takes a row lock');

        $this->migration('append_only')->up();
    }
}
