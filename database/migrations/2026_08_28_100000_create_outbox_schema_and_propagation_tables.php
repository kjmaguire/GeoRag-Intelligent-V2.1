<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Port `outbox.pending_propagations` + `outbox.propagation_attempts` — and the
 * `outbox` schema itself — into the migration chain.
 *
 * ## Why these do not exist on Azure
 *
 * Both tables are declared only in `database/raw/phase0/40-layer-d-outbox.sql`,
 * which CD never runs: `cd.yml` executes `php artisan migrate` and nothing
 * else, and `php artisan db:apply-raw` is a manual operator step
 * (`ops/runbooks/raw-sql-layer.md`). They are the first two entries in
 * `scripts/raw-parity-baseline.txt`.
 *
 * The schema is worse than the tables. `outbox` is created by
 * `docker/postgresql/init/10-phase0-extensions-and-schemas.sql` — a Docker
 * image init script that runs once on a fresh container volume. Azure uses a
 * managed Postgres Flexible Server, so that file has never executed there.
 * `outbox` is the only schema in that init script with no counterpart in the
 * migration chain, which is why this migration creates it rather than assuming
 * it.
 *
 * ## What this unblocks
 *
 * `outbox_dispatcher` is registered in the Hatchet ingestion pool on a
 * `* * * * *` cron — it polls `pending_propagations` every minute and has
 * therefore been erroring on every tick in production. `store_reconciliation`,
 * `tenant_isolation_auditor`, `support_packet` and `app/metrics.py` read the
 * same tables.
 *
 * ## RLS: fail-CLOSED, deliberately not the raw file's shape
 *
 * The raw file gets its policy from `phase0/95-rls-policies.sql`, whose macro
 * writes the fail-open "admin escape hatch" shape (an unset
 * `app.workspace_id` admits every row). That is NOT what these two tables are
 * supposed to carry any more:
 * `2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset` lists both
 * in its verified-safe-to-flip subset and writes the closed form — but it
 * skips them at runtime precisely because they do not exist, so the flip has
 * never been applied. `WorkspaceRlsCoverageTest::
 * test_verified_subset_has_no_fail_open_escape_hatch` asserts the closed shape
 * for both, and that assertion has been passing vacuously.
 *
 * So this migration creates them already closed, in the exact shape the
 * 08-14 migration writes. Creating them fail-open would satisfy "match the raw
 * file" while reintroducing a shape the project has already decided against
 * and turning a vacuous test green into a real failure.
 *
 * `FORCE ROW LEVEL SECURITY` is set alongside `ENABLE` because
 * `2026_08_24_010000_force_row_level_security_on_all_rls_enabled_tables` is a
 * one-shot catalog sweep that has already run — a table created afterwards
 * with only `ENABLE` would leave the `georag` owner (what
 * `MIGRATE_DB_USERNAME` connects as) bypassing the policy entirely, and
 * `WorkspaceRlsCoverageTest::test_every_rls_enabled_table_is_forced` fails on
 * exactly that.
 *
 * ## Fidelity note
 *
 * `target_store`'s CHECK constraint is ported verbatim and still admits
 * `'neo4j'`, a store removed on 2026-07-28. Narrowing it is a data-migration
 * question (existing rows on a dev cluster may carry that value), not a
 * schema-parity one, so it is left for a follow-up rather than silently
 * changed here.
 *
 * Idempotent: `CREATE SCHEMA/TABLE/INDEX IF NOT EXISTS` plus
 * `DROP POLICY IF EXISTS` before each `CREATE POLICY`.
 */
return new class extends Migration
{
    /** Tables that carry the closed `tenant_isolation` policy. */
    private const RLS_TABLES = ['pending_propagations', 'propagation_attempts'];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE SCHEMA IF NOT EXISTS outbox');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS outbox.pending_propagations (
                id                            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id                  uuid        NULL,
                source_schema                 text        NOT NULL,
                source_table                  text        NOT NULL,
                source_id                     text        NOT NULL,
                target_store                  text        NOT NULL
                    CHECK (target_store IN ('qdrant','neo4j','seaweedfs','redis','external_webhook')),
                target_collection             text        NULL,
                operation                     text        NOT NULL
                    CHECK (operation IN ('upsert','delete','reindex')),
                payload                       jsonb       NOT NULL DEFAULT '{}'::jsonb,
                idempotency_key               text        NOT NULL,
                target_store_concurrency_hint smallint    NOT NULL DEFAULT 4,
                status                        text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','in_flight','succeeded','dead_lettered')),
                enqueued_at                   timestamptz NOT NULL DEFAULT now(),
                last_attempted_at             timestamptz NULL,
                succeeded_at                  timestamptz NULL,
                dead_lettered_at              timestamptz NULL,
                audit_ledger_ref              uuid        NULL
            )
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON TABLE outbox.pending_propagations IS
                'One row per multi-store write awaiting dispatch to a secondary store.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN outbox.pending_propagations.idempotency_key IS
                'Stable key the dispatcher uses to dedupe — derived from (target_store, target_collection, source_id, operation).'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN outbox.pending_propagations.target_store_concurrency_hint IS
                'Max concurrent attempts in flight against this target; dispatcher reads per-row.'
        SQL);

        // Idempotency: the same logical write must not be enqueued twice while
        // an earlier copy is still pending or in flight.
        DB::statement(<<<'SQL'
            CREATE UNIQUE INDEX IF NOT EXISTS pending_propagations_idempotency_unique
                ON outbox.pending_propagations (target_store, idempotency_key)
                WHERE status IN ('pending','in_flight')
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS pending_propagations_dispatch_idx
                ON outbox.pending_propagations (target_store, status, enqueued_at)
                WHERE status IN ('pending','in_flight')
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS pending_propagations_workspace_idx
                ON outbox.pending_propagations (workspace_id, enqueued_at DESC)
                WHERE workspace_id IS NOT NULL
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS pending_propagations_source_idx
                ON outbox.pending_propagations (source_schema, source_table, source_id)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS pending_propagations_dead_letter_idx
                ON outbox.pending_propagations (dead_lettered_at DESC)
                WHERE status = 'dead_lettered'
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS outbox.propagation_attempts (
                id               bigserial   PRIMARY KEY,
                propagation_id   uuid        NOT NULL
                    REFERENCES outbox.pending_propagations(id) ON DELETE CASCADE,
                workspace_id     uuid        NULL,
                attempt_no       smallint    NOT NULL,
                status           text        NOT NULL
                    CHECK (status IN ('success','transient_failure','permanent_failure','dead_lettered')),
                error_kind       text        NULL,
                error_message    text        NULL,
                error_detail     jsonb       NULL,
                started_at       timestamptz NOT NULL DEFAULT now(),
                finished_at      timestamptz NULL,
                duration_ms      bigint      GENERATED ALWAYS AS (
                                     CASE WHEN finished_at IS NULL THEN NULL
                                          ELSE EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000 END
                                 ) STORED,
                audit_ledger_ref uuid        NULL,
                CONSTRAINT propagation_attempts_propagation_attempt_no
                    UNIQUE (propagation_id, attempt_no)
            )
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON TABLE outbox.propagation_attempts IS
                'Per-attempt record of secondary-store writes. Several rows per propagation_id is normal (retries).'
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS propagation_attempts_propagation_idx
                ON outbox.propagation_attempts (propagation_id, attempt_no)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS propagation_attempts_status_idx
                ON outbox.propagation_attempts (status, started_at DESC)
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS propagation_attempts_workspace_idx
                ON outbox.propagation_attempts (workspace_id, started_at DESC)
                WHERE workspace_id IS NOT NULL
        SQL);

        foreach (self::RLS_TABLES as $table) {
            $qualified = "outbox.{$table}";

            DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS tenant_isolation ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY tenant_isolation ON {$qualified}
                    USING (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                    WITH CHECK (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                SQL);
        }

        // Grants are role-conditional: georag_app exists on the deployed
        // clusters but not necessarily on a throwaway CI database.
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    GRANT USAGE ON SCHEMA outbox TO georag_app;
                    GRANT SELECT, INSERT, UPDATE, DELETE
                        ON outbox.pending_propagations, outbox.propagation_attempts
                        TO georag_app;
                    GRANT USAGE, SELECT ON SEQUENCE outbox.propagation_attempts_id_seq TO georag_app;
                END IF;
            END $$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // propagation_attempts first — it FKs pending_propagations.
        DB::statement('DROP TABLE IF EXISTS outbox.propagation_attempts');
        DB::statement('DROP TABLE IF EXISTS outbox.pending_propagations');

        // The schema is left in place: it predates this migration on every
        // Docker-provisioned cluster, and dropping it there would remove
        // something this migration did not create.
    }
};
