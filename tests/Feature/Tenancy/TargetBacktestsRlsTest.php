<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * targeting.target_backtests after 2026_09_29_190000: platform-wide rows
 * (NULL workspace_id) stay visible everywhere, tenant rows only to their own
 * workspace, and an UNSET GUC no longer means "every workspace".
 *
 * Read as georag_app (NOBYPASSRLS) via SET LOCAL ROLE inside a transaction
 * that is rolled back, like FailClosedRlsPolicyTest.
 */
final class TargetBacktestsRlsTest extends TestCase
{
    private const WS_A = '5ec30000-0000-4000-8000-00000000000a';

    private const WS_B = '5ec30000-0000-4000-8000-00000000000b';

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }
        if (DB::selectOne("SELECT to_regclass('targeting.target_backtests') IS NOT NULL AS present")?->present !== true) {
            $this->markTestSkipped('targeting.target_backtests is absent from this cluster.');
        }
        if (DB::selectOne("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app' AND NOT rolbypassrls) AS present")?->present !== true) {
            $this->markTestSkipped('georag_app role not provisioned.');
        }
    }

    #[Test]
    public function exactly_one_permissive_policy_and_no_unbound_guc_branch(): void
    {
        $policies = DB::select(
            "SELECT polname, polpermissive, pg_get_expr(polqual, polrelid) AS qual
               FROM pg_policy WHERE polrelid = 'targeting.target_backtests'::regclass",
        );

        $this->assertCount(1, $policies, 'Permissive policies are OR-ed; a second one can reopen what the first closes.');
        $this->assertStringNotContainsString('IS NULL) OR (workspace_id IS NULL', (string) $policies[0]->qual);
        $this->assertStringNotContainsString("NULLIF(current_setting('app.workspace_id'::text, true), ''::text) IS NULL", (string) $policies[0]->qual);
    }

    #[Test]
    public function an_unbound_session_sees_only_platform_rows(): void
    {
        DB::beginTransaction();

        try {
            // Owner-side setup with FK triggers off: the probe is about RLS,
            // not about the model-version chain behind model_version_id.
            DB::statement('SET LOCAL session_replication_role = replica');
            DB::insert(
                "INSERT INTO targeting.target_backtests
                    (model_version_id, workspace_id, window_start, window_end, metrics_payload)
                 VALUES (gen_random_uuid(), ?::uuid, now() - interval '1 day', now(), '{\"probe\":\"sec\"}'),
                        (gen_random_uuid(), ?::uuid, now() - interval '1 day', now(), '{\"probe\":\"sec\"}'),
                        (gen_random_uuid(), NULL,    now() - interval '1 day', now(), '{\"probe\":\"sec\"}')",
                [self::WS_A, self::WS_B],
            );
            DB::statement('SET LOCAL session_replication_role = origin');
            DB::statement('SET LOCAL ROLE georag_app');

            $this->assertSame(['platform'], $this->visible(), 'unbound GUC');

            DB::statement("SELECT set_config('app.workspace_id', '', true)");
            $this->assertSame(['platform'], $this->visible(), 'empty GUC');

            DB::statement("SELECT set_config('app.workspace_id', ?, true)", [self::WS_A]);
            $this->assertSame([self::WS_A, 'platform'], $this->visible(), 'bound to A');

            DB::statement("SELECT set_config('app.workspace_id', ?, true)", [self::WS_B]);
            $this->assertSame([self::WS_B, 'platform'], $this->visible(), 'bound to B');
        } finally {
            DB::rollBack();
        }
    }

    /**
     * @return list<string>
     */
    private function visible(): array
    {
        return array_map(
            static fn (object $r): string => (string) $r->ws,
            DB::select(
                "SELECT COALESCE(workspace_id::text, 'platform') AS ws
                   FROM targeting.target_backtests
                  WHERE metrics_payload->>'probe' = 'sec'
                  ORDER BY 1",
            ),
        );
    }
}
