<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.samples is the one core silver table whose tenant column is still
 * optional and unconstrained (database review 2026-10, section 06b).
 *
 * Every sibling that carries workspace_id -- collars, surveys, lithology_logs,
 * geochemistry, reports, well_log_curves, the typed drill tables -- has it
 * NOT NULL with a foreign key to silver.workspaces ON DELETE CASCADE.
 * silver.samples has neither: `workspace_id uuid NULL`, no FK (it was added by
 * 2026_05_25_184335_provision_silver_workspace_columns_for_test_db as a bare
 * column and the phase0 raw sweeps never touched this table).
 *
 * What that allows today:
 *   - a row inserted without workspace_id, which no tenant can ever read once
 *     the session GUC is set (the policy compares workspace_id to it) and
 *     which every unset-GUC session reads -- an orphan outside any tenant;
 *   - a row whose workspace_id disagrees with its collar's workspace_id, or
 *     names a workspace that does not exist, because nothing relates the two.
 * Both writers in the repo (hatchet_workflows/ingest_tabular.py and
 * services/ingest/derive_intervals.py) supply workspace_id, so this closes the
 * gap rather than changing behaviour.
 *
 * What this migration does, in order, on pgsql only:
 *
 *   1. Backfills workspace_id from the sample's collar where it is NULL.
 *   2. Installs a BEFORE INSERT trigger that fills a NULL workspace_id from the
 *      collar, so a legacy writer that omits the column keeps working once the
 *      NOT NULL below lands (the same pattern as
 *      2026_05_25_175601_add_workspace_id_autopopulation_trigger_to_bronze_
 *      provenance). The lookup is an ordinary invoker-rights read of
 *      silver.collars: under RLS a session scoped to workspace A cannot resolve
 *      a workspace-B collar, the column stays NULL and the NOT NULL rejects the
 *      insert -- the trigger can never launder a row into another tenant.
 *      An explicitly supplied workspace_id is never overwritten.
 *   3. Adds the FK to silver.workspaces(workspace_id) ON DELETE CASCADE, only if
 *      no existing row names a missing workspace.
 *   4. Sets NOT NULL, only if no NULL remains after the backfill (a sample whose
 *      collar is gone cannot exist -- silver_samples_collar_id_foreign cascades --
 *      so in practice this always holds).
 *
 * Steps 3 and 4 are each skipped with a WARNING, not a failure, if existing data
 * would violate them: a deploy must not be blocked by a legacy row; the WARNING
 * names the statement to run by hand once the rows are repaired. The migration
 * is idempotent and re-runnable.
 *
 * Tenant isolation: strictly tighter. No policy or grant changes; the existing
 * samples_workspace_isolation_v2 policy keeps governing reads and writes.
 *
 * A non-owner migrating role (the "Migrations under production privileges" CI
 * job) is handled: each step runs in a block that catches insufficient_privilege
 * and logs a NOTICE instead of aborting.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $present = DB::selectOne(
            "SELECT EXISTS (
                SELECT 1 FROM pg_attribute
                 WHERE attrelid = to_regclass('silver.samples')
                   AND attname = 'workspace_id'
                   AND NOT attisdropped
            ) AS present",
        )->present;

        if (! $present || DB::selectOne("SELECT to_regclass('silver.collars') AS rel")->rel === null) {
            return;
        }

        DB::unprepared(<<<'SQL'
            DO $do$
            DECLARE
                v_orphans bigint;
                v_nulls   bigint;
            BEGIN
                -- 1. Backfill from the collar.
                BEGIN
                    UPDATE silver.samples s
                       SET workspace_id = c.workspace_id
                      FROM silver.collars c
                     WHERE c.collar_id = s.collar_id
                       AND s.workspace_id IS NULL;
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE NOTICE 'silver.samples: not permitted to backfill workspace_id; run the UPDATE by hand as the owner';
                END;

                -- 2. Autopopulate on insert.
                BEGIN
                    CREATE OR REPLACE FUNCTION silver.samples_default_workspace_id()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SET search_path = pg_catalog, silver
                    AS $fn$
                    BEGIN
                        IF NEW.workspace_id IS NULL THEN
                            SELECT c.workspace_id
                              INTO NEW.workspace_id
                              FROM silver.collars c
                             WHERE c.collar_id = NEW.collar_id;
                        END IF;
                        RETURN NEW;
                    END
                    $fn$;

                    DROP TRIGGER IF EXISTS trg_samples_default_workspace_id ON silver.samples;
                    CREATE TRIGGER trg_samples_default_workspace_id
                        BEFORE INSERT ON silver.samples
                        FOR EACH ROW
                        EXECUTE FUNCTION silver.samples_default_workspace_id();
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE NOTICE 'silver.samples: not permitted to install the workspace_id autopopulation trigger; apply it by hand as the owner';
                END;

                -- 3. Foreign key, if the data allows it.
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conrelid = 'silver.samples'::regclass
                       AND contype = 'f'
                       AND conname = 'samples_workspace_id_fkey'
                ) THEN
                    SELECT count(*) INTO v_orphans
                      FROM silver.samples s
                     WHERE s.workspace_id IS NOT NULL
                       AND NOT EXISTS (
                            SELECT 1 FROM silver.workspaces w
                             WHERE w.workspace_id = s.workspace_id
                       );

                    IF v_orphans = 0 THEN
                        BEGIN
                            ALTER TABLE silver.samples
                                ADD CONSTRAINT samples_workspace_id_fkey
                                FOREIGN KEY (workspace_id)
                                REFERENCES silver.workspaces (workspace_id)
                                ON DELETE CASCADE;
                        EXCEPTION WHEN insufficient_privilege THEN
                            RAISE NOTICE 'silver.samples: not permitted to add samples_workspace_id_fkey; apply it by hand as the owner';
                        END;
                    ELSE
                        RAISE WARNING 'silver.samples: % row(s) name a workspace_id absent from silver.workspaces; samples_workspace_id_fkey NOT added. Repair them, then ALTER TABLE silver.samples ADD CONSTRAINT samples_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES silver.workspaces (workspace_id) ON DELETE CASCADE', v_orphans;
                    END IF;
                END IF;

                -- 4. NOT NULL, if the data allows it.
                IF EXISTS (
                    SELECT 1 FROM pg_attribute
                     WHERE attrelid = 'silver.samples'::regclass
                       AND attname = 'workspace_id'
                       AND NOT attnotnull
                ) THEN
                    SELECT count(*) INTO v_nulls FROM silver.samples WHERE workspace_id IS NULL;

                    IF v_nulls = 0 THEN
                        BEGIN
                            ALTER TABLE silver.samples ALTER COLUMN workspace_id SET NOT NULL;
                        EXCEPTION WHEN insufficient_privilege THEN
                            RAISE NOTICE 'silver.samples: not permitted to SET NOT NULL on workspace_id; apply it by hand as the owner';
                        END;
                    ELSE
                        RAISE WARNING 'silver.samples: % row(s) still have a NULL workspace_id; NOT NULL NOT applied. Repair them, then ALTER TABLE silver.samples ALTER COLUMN workspace_id SET NOT NULL', v_nulls;
                    END IF;
                END IF;
            END
            $do$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (DB::selectOne("SELECT to_regclass('silver.samples') AS rel")->rel === null) {
            return;
        }

        DB::unprepared('DROP TRIGGER IF EXISTS trg_samples_default_workspace_id ON silver.samples');
        DB::unprepared('DROP FUNCTION IF EXISTS silver.samples_default_workspace_id()');
        DB::unprepared('ALTER TABLE silver.samples DROP CONSTRAINT IF EXISTS samples_workspace_id_fkey');
        DB::unprepared('ALTER TABLE silver.samples ALTER COLUMN workspace_id DROP NOT NULL');
    }
};
