<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Lock down the SECURITY DEFINER functions that handle secrets and the MV
 * refresh (database audit 2026-10).
 *
 * Two defects, on the same seven functions:
 *
 *  1. EXECUTE was granted to PUBLIC. PostgreSQL grants it to PUBLIC by default
 *     on every new function, and none of the creating migrations revoked it,
 *     so ANY role that can connect -- martin_readonly, the hatchet role,
 *     georag_read -- could call workflow.get_flow_jwt_keys() (which RETURNS
 *     the decrypted per-flow JWT secrets) or usage.register_external_
 *     notification_sender(), subject only to knowing the
 *     app.audit_encryption_key GUC. Revoked from PUBLIC; the georag_app grant
 *     (the role every application service connects as, in compose and on ECS)
 *     is kept and re-asserted, and the owner keeps EXECUTE implicitly.
 *
 *     Callers, confirmed in the repo: Laravel (IntegrationsController ->
 *     set_flow_jwt_secret, register_external_notification_sender) and FastAPI
 *     (services/flow_jwt.py -> get_flow_jwt_keys; hatchet external_notification
 *     -> lookup_external_notification_sender_secrets; flow_jwt_key_reaper ->
 *     reap_expired_flow_jwt_keys; mv_refresh_silver -> refresh_silver_agent_mvs)
 *     all connect as georag_app. get_flow_jwt_secret has no caller outside its
 *     own migrations.
 *
 *  2. `SET search_path = <schema>, public, pg_catalog` puts public BEFORE
 *     pg_catalog. In a SECURITY DEFINER function that lets anyone who can
 *     create objects in `public` shadow a built-in the body calls with their
 *     own, which then runs with the owner's privileges. pg_catalog now leads.
 *
 *       - refresh_silver_agent_mvs: `pg_catalog, workflow` (already set by
 *         2026_10_04_200100; re-asserted here so this migration stands alone).
 *         Its body is fully schema-qualified.
 *       - the six pgcrypto-using functions: `pg_catalog, <schema>, public`.
 *         `public` STAYS, but last, because their bodies call pgp_sym_encrypt /
 *         pgp_sym_decrypt UNQUALIFIED and pgcrypto lives in public
 *         (2026_08_17_070000 forces it there). Dropping public here -- the
 *         tempting `pg_catalog, <schema>` -- would make every call fail with
 *         "function pgp_sym_decrypt(bytea, text) does not exist" the moment
 *         the setting took effect. Qualifying those calls as `public.pgp_...`
 *         and dropping public entirely is the stricter follow-up; it means
 *         replacing the function bodies, which are defined in four places
 *         (raw phase4/5/6/7 and the test-DB provisioning migrations) and are
 *         deliberately not rewritten here.
 *
 * ALTER FUNCTION only: bodies, signatures, owners and results are untouched,
 * and a function that does not exist on this cluster is skipped.
 *
 * Out of scope but worth knowing: silver.significant_intersections_by_project
 * (a Martin MVT function) is SECURITY DEFINER with search_path
 * `pg_catalog, public` and default PUBLIC EXECUTE; it is called by
 * martin_readonly, so revoking it needs that role's grant added first.
 *
 * pgsql only. down() restores the previous search_path and the PUBLIC grant.
 */
return new class extends Migration
{
    /**
     * signature => [search_path after, search_path before].
     *
     * @var array<string, array{0: string, 1: string}>
     */
    private const FUNCTIONS = [
        'workflow.get_flow_jwt_secret(text)' => ['pg_catalog, workflow, public', 'workflow, public, pg_catalog'],
        'workflow.get_flow_jwt_keys(text)' => ['pg_catalog, workflow, public', 'workflow, public, pg_catalog'],
        'workflow.set_flow_jwt_secret(text, text, text, integer)' => ['pg_catalog, workflow, public', 'workflow, public, pg_catalog'],
        'workflow.reap_expired_flow_jwt_keys(integer)' => ['pg_catalog, workflow, public', 'workflow, public, pg_catalog'],
        'usage.lookup_external_notification_sender_secrets(text)' => ['pg_catalog, usage, public', 'usage, public, pg_catalog'],
        'usage.register_external_notification_sender(text, text, text, text, uuid)' => ['pg_catalog, usage, public', 'usage, public, pg_catalog'],
        'workflow.refresh_silver_agent_mvs()' => ['pg_catalog, workflow', 'workflow, silver, public, pg_catalog'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $hasAppRole = DB::selectOne("SELECT 1 AS present FROM pg_roles WHERE rolname = 'georag_app'") !== null;

        foreach (self::FUNCTIONS as $signature => [$after]) {
            if (! $this->functionExists($signature)) {
                continue;
            }

            DB::statement("ALTER FUNCTION {$signature} SET search_path = {$after}");
            DB::statement("REVOKE EXECUTE ON FUNCTION {$signature} FROM PUBLIC");

            if ($hasAppRole) {
                DB::statement("GRANT EXECUTE ON FUNCTION {$signature} TO georag_app");
            }
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::FUNCTIONS as $signature => [, $before]) {
            if (! $this->functionExists($signature)) {
                continue;
            }

            DB::statement("ALTER FUNCTION {$signature} SET search_path = {$before}");
            DB::statement("GRANT EXECUTE ON FUNCTION {$signature} TO PUBLIC");
        }
    }

    private function functionExists(string $signature): bool
    {
        return DB::selectOne('SELECT to_regprocedure(?) IS NOT NULL AS present', [$signature])->present;
    }
};
