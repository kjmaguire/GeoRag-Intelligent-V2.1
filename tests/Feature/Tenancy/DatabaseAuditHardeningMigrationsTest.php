<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * What 2026_10_04_2002xx .. 2006xx leave behind, read back from the catalogs
 * and exercised, not from the migration text:
 *
 *   200100  workflow.refresh_silver_agent_mvs() refreshes CONCURRENTLY
 *   200200  created_at / pending-embed / trigram indexes
 *   200300  audit-ledger external_notification partial index
 *   200400  FK-column indexes
 *   200500  SECURITY DEFINER functions: no PUBLIC EXECUTE, pg_catalog first
 *   200600  georag_app statement_timeout / lock_timeout
 *
 * Tenant isolation is untouched by any of them (indexes, function ACLs and a
 * role setting; no policy or column changes) -- WorkspaceRlsCoverageTest and
 * FailClosedRlsPolicyTest are the guards for that and are unaffected.
 *
 * Postgres only. Index tests skip for a table a given cluster lacks.
 */
final class DatabaseAuditHardeningMigrationsTest extends TestCase
{
    use RequiresPostgres;

    /**
     * @return array<string, array{0: string, 1: string, 2: string}>
     */
    public static function indexes(): array
    {
        return [
            'collars created_at' => ['silver.collars', 'idx_collars_created_at', '(created_at DESC)'],
            'samples created_at' => ['silver.samples', 'idx_samples_created_at', '(created_at DESC)'],
            'lithology created_at' => ['silver.lithology_logs', 'idx_lithology_logs_created_at', '(created_at DESC)'],
            'pending embed backlog' => ['silver.document_passages', 'idx_document_passages_pending_created', '(workspace_id, created_at, passage_id) WHERE (embedding_id IS NULL)'],
            'passage text trigram' => ['silver.document_passages', 'idx_document_passages_text_trgm', 'USING gin (text gin_trgm_ops)'],
            'alias trigram' => ['silver.entity_aliases', 'idx_entity_aliases_norm_trgm', 'USING gin (alias_normalised gin_trgm_ops)'],
            'external notification dedup' => ['audit.audit_ledger', 'audit_ledger_external_notification_id_idx', "WHERE (action_type = 'external_notification.received'::text)"],
            'zones project' => ['targeting.target_candidate_zones', 'idx_target_candidate_zones_project_id', '(project_id)'],
            'zones model' => ['targeting.target_candidate_zones', 'idx_target_candidate_zones_target_model_id', '(target_model_id)'],
            'uncertainties score' => ['targeting.target_uncertainties', 'idx_target_uncertainties_score_id', '(score_id)'],
            'recommendations zone' => ['targeting.target_recommendations', 'idx_target_recommendations_zone_id', '(zone_id)'],
            'backtests version' => ['targeting.target_backtests', 'idx_target_backtests_model_version_id', '(model_version_id)'],
            'geophysics lines' => ['silver.geophysics_lines', 'idx_geophysics_lines_project_id', '(project_id)'],
            'geophysics channels' => ['silver.geophysics_line_channels', 'idx_geophysics_line_channels_project_id', '(project_id)'],
            'dcip observations' => ['silver.geophysics_dcip_observations', 'idx_geophysics_dcip_observations_project_id', '(project_id)'],
            'dcip models' => ['silver.geophysics_dcip_models', 'idx_geophysics_dcip_models_project_id', '(project_id)'],
            'las pending workspace' => ['silver.las_pending_collar', 'idx_las_pending_collar_workspace_id', '(workspace_id)'],
            'citation spans workspace' => ['silver.answer_citation_spans', 'idx_answer_citation_spans_workspace_id', '(workspace_id)'],
            'comment parent' => ['silver.collab_comments', 'idx_collab_comments_parent_comment_id', '(parent_comment_id)'],
        ];
    }

    #[Test]
    #[DataProvider('indexes')]
    public function index_exists_is_valid_and_has_the_intended_shape(string $table, string $index, string $fragment): void
    {
        if (! DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$table])->present) {
            $this->markTestSkipped("{$table} is absent from this cluster.");
        }

        $row = DB::selectOne(
            'SELECT i.indisvalid AS valid, pg_get_indexdef(c.oid) AS def
               FROM pg_index i
               JOIN pg_class c ON c.oid = i.indexrelid
              WHERE c.relname = ? AND i.indrelid = to_regclass(?)',
            [$index, $table],
        );

        $this->assertNotNull($row, "{$index} does not exist on {$table}");
        $this->assertTrue($row->valid, "{$index} is INVALID (a CONCURRENTLY build that failed)");
        $this->assertStringContainsString($fragment, $row->def);
    }

    #[Test]
    public function the_agent_mv_refresh_is_concurrent_and_still_runs(): void
    {
        $def = DB::selectOne(
            "SELECT pg_get_functiondef('workflow.refresh_silver_agent_mvs()'::regprocedure) AS def",
        )->def;

        $this->assertStringContainsString('REFRESH MATERIALIZED VIEW CONCURRENTLY silver.mv_collar_summary', $def);

        // The unique index CONCURRENTLY needs, on the view itself.
        $this->assertTrue(DB::selectOne(
            "SELECT EXISTS (
                SELECT 1 FROM pg_index i
                 WHERE i.indrelid = 'silver.mv_collar_summary'::regclass
                   AND i.indisunique AND i.indisvalid AND i.indpred IS NULL
             ) AS ok",
        )->ok);

        DB::beginTransaction();
        try {
            $rows = DB::select('SELECT mv_name FROM workflow.refresh_silver_agent_mvs()');
            $this->assertSame(['silver.mv_collar_summary'], array_column($rows, 'mv_name'));
        } finally {
            DB::rollBack();
        }
    }

    /**
     * @return array<string, array{0: string}>
     */
    public static function definerFunctions(): array
    {
        $out = [];
        foreach ([
            'workflow.get_flow_jwt_secret(text)',
            'workflow.get_flow_jwt_keys(text)',
            'workflow.set_flow_jwt_secret(text,text,text,integer)',
            'workflow.reap_expired_flow_jwt_keys(integer)',
            'usage.lookup_external_notification_sender_secrets(text)',
            'usage.register_external_notification_sender(text,text,text,text,uuid)',
            'workflow.refresh_silver_agent_mvs()',
        ] as $sig) {
            $out[$sig] = [$sig];
        }

        return $out;
    }

    #[Test]
    #[DataProvider('definerFunctions')]
    public function definer_function_is_not_executable_by_public_and_has_a_safe_search_path(string $signature): void
    {
        $row = DB::selectOne(
            "SELECT p.prosecdef AS definer,
                    p.proconfig AS config,
                    EXISTS (
                        SELECT 1
                          FROM aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a
                         WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'
                    ) AS public_exec
               FROM pg_proc p
              WHERE p.oid = to_regprocedure(?)",
            [$signature],
        );

        if ($row === null) {
            $this->markTestSkipped("{$signature} is absent from this cluster.");
        }

        $this->assertTrue($row->definer, "{$signature} is no longer SECURITY DEFINER");
        $this->assertFalse($row->public_exec, "{$signature} is executable by PUBLIC");
        $this->assertMatchesRegularExpression(
            '/search_path=pg_catalog, /',
            (string) $row->config,
            "{$signature} search_path does not lead with pg_catalog: {$row->config}",
        );

        $hasApp = DB::selectOne("SELECT 1 AS present FROM pg_roles WHERE rolname = 'georag_app'");
        if ($hasApp !== null) {
            $this->assertTrue(
                DB::selectOne('SELECT has_function_privilege(?, to_regprocedure(?), ?) AS ok', ['georag_app', $signature, 'EXECUTE'])->ok,
                "georag_app lost EXECUTE on {$signature}",
            );
        }
    }

    /**
     * The reason `public` stays LAST in the search_path of the six pgcrypto
     * functions: a bare `pg_catalog, <schema>` would break pgp_sym_encrypt /
     * pgp_sym_decrypt, which these bodies call unqualified.
     */
    #[Test]
    public function secret_functions_still_encrypt_and_decrypt_under_the_new_search_path(): void
    {
        $flow = DB::selectOne('SELECT flow_name FROM workflow.flow_registry ORDER BY flow_name LIMIT 1');
        if ($flow === null) {
            $this->markTestSkipped('workflow.flow_registry has no seeded flow.');
        }

        DB::beginTransaction();
        try {
            DB::statement("SELECT set_config('app.audit_encryption_key', 'test-only-key', true)");

            DB::select('SELECT workflow.set_flow_jwt_secret(?, ?, ?, 0)', [$flow->flow_name, 'kid-t', 'plain-t']);
            $keys = DB::select('SELECT kid, plain FROM workflow.get_flow_jwt_keys(?)', [$flow->flow_name]);
            $this->assertSame('plain-t', $keys[0]->plain);

            DB::select("SELECT usage.register_external_notification_sender('src-hardening', 'kid-n', 'plain-n', NULL, NULL)");
            $senders = DB::select("SELECT secret_plain FROM usage.lookup_external_notification_sender_secrets('src-hardening')");
            $this->assertSame('plain-n', $senders[0]->secret_plain);
        } finally {
            DB::rollBack();
        }
    }

    #[Test]
    public function the_application_role_has_bounded_statement_and_lock_timeouts(): void
    {
        $role = DB::selectOne("SELECT rolconfig FROM pg_roles WHERE rolname = 'georag_app'");
        if ($role === null) {
            $this->markTestSkipped('georag_app is not provisioned on this cluster.');
        }

        $config = (string) $role->rolconfig;

        // The migration never loosens: a stricter server-level value suppresses
        // the role setting, so only assert where the server leaves it unbounded.
        foreach (['statement_timeout' => '300s', 'lock_timeout' => '10s'] as $name => $value) {
            $serverMs = (int) DB::selectOne('SELECT setting FROM pg_settings WHERE name = ?', [$name])->setting;
            if ($serverMs === 0) {
                $this->assertStringContainsString("{$name}={$value}", $config);
            } else {
                $this->assertStringNotContainsString("{$name}=", $config, "{$name} is already bounded server-side; the role must not loosen it");
            }
        }
    }
}
