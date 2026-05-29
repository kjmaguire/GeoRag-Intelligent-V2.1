<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Cosmetic catalog cleanup 2026-05-25 — normalize workspace_isolation
 * RLS policies that accumulated redundant nested `NULLIF(...) IS NULL OR`
 * clauses from layered migrations over the past few months.
 *
 * **The shape we want (canonical):**
 *
 *     USING (
 *       NULLIF(current_setting('app.workspace_id', true), '') IS NULL
 *       OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *     )
 *
 * **The shape we have on 61 tables today** (representative example):
 *
 *     USING (
 *       NULLIF(...) IS NULL OR
 *         (NULLIF(...) IS NULL OR
 *           workspace_id = NULLIF(...)::uuid)
 *     )
 *
 * The extra `NULLIF IS NULL` clauses are dead branches — already
 * covered by the outermost one. Worst-case shape (e.g.
 * silver.decision_records) layers FIVE NULLIFs plus a redundant
 * `NULLIF(...) = ''` check that's always-false. Functionally
 * equivalent to the canonical; just noise.
 *
 * **Strategy.** Self-discovering DO block:
 *
 *   1. Iterate every policy named `*_workspace_isolation` (or the
 *      legacy `tenant_isolation` on tables still carrying it) whose
 *      qual contains 3+ NULLIF calls — anything beyond canonical's 2.
 *   2. DROP POLICY + CREATE POLICY with the canonical shape.
 *   3. Only touch tables whose workspace_id column is NOT NULL
 *      (otherwise the canonical needs the OR workspace_id IS NULL
 *      exemption, which is a different policy shape — leave those
 *      alone).
 *
 * **Behavior preservation.** Every layered policy is logically
 * equivalent to the canonical (`A OR (A OR B)` = `A OR B`). The
 * `NULLIF(...) = ''` branches in the worst-case shape are unreachable
 * (NULLIF(x, '') returns NULL when x = '', and NULL = '' is NULL).
 * Net behavior change: zero. Net catalog row change: 61 cleaner qual
 * strings.
 *
 * Pre/post audit query:
 *
 *     SELECT COUNT(*) FROM pg_policies
 *      WHERE qual::text LIKE '%NULLIF%'
 *        AND (LENGTH(qual::text) - LENGTH(REPLACE(qual::text, 'NULLIF', '')))
 *            / LENGTH('NULLIF') >= 3;
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::unprepared(<<<'SQL'
            DO $$
            DECLARE
                rec RECORD;
                is_nullable text;
            BEGIN
                FOR rec IN
                    SELECT p.schemaname, p.tablename, p.policyname
                      FROM pg_policies p
                     WHERE p.schemaname IN ('silver','gold','bronze','audit','public_geo','index')
                       AND (p.policyname LIKE '%_workspace_isolation%'
                            OR p.policyname IN ('tenant_isolation'))
                       AND (LENGTH(p.qual::text) - LENGTH(REPLACE(p.qual::text, 'NULLIF', '')))
                           / LENGTH('NULLIF') >= 3
                LOOP
                    -- Skip if the table's workspace_id column is nullable
                    -- — those policies legitimately carry the
                    -- `OR workspace_id IS NULL` exemption (different shape).
                    SELECT c.is_nullable INTO is_nullable
                      FROM information_schema.columns c
                     WHERE c.table_schema = rec.schemaname
                       AND c.table_name = rec.tablename
                       AND c.column_name = 'workspace_id';

                    IF is_nullable IS NULL OR is_nullable = 'YES' THEN
                        CONTINUE;
                    END IF;

                    EXECUTE format(
                        'DROP POLICY IF EXISTS %I ON %I.%I',
                        rec.policyname, rec.schemaname, rec.tablename
                    );
                    EXECUTE format(
                        $f$
                        CREATE POLICY %I ON %I.%I
                          USING (
                            NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                            OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                          )
                        $f$,
                        rec.policyname, rec.schemaname, rec.tablename
                    );
                END LOOP;
            END
            $$;
        SQL);
    }

    public function down(): void
    {
        // No-op — the original layered policies were never functionally
        // distinct from the canonical we replaced them with. Recreating
        // the noise would serve no purpose. If a future audit needs
        // the pre-cleanup state, restore from the migration's pre-run
        // pg_dump.
    }
};
