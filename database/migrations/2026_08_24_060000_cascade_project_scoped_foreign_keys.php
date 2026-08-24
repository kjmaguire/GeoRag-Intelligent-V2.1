<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Make every project-scoped foreign key CASCADE, so deleting a project cannot
 * be blocked by a child row.
 *
 * ## The bug
 *
 * `silver.collars` cascades from `silver.projects`
 * (2026_04_09_180100_create_collars_table.php:18), but thirteen drillhole
 * child tables declare `collar_id uuid NOT NULL REFERENCES
 * silver.collars(collar_id),` with NO `ON DELETE` clause. The SQL default is
 * NO ACTION, which BLOCKS the parent delete rather than following it. The
 * cascade from `silver.projects` reaches `silver.collars`, the child FK
 * refuses, and `ProjectController::destroy`'s wrapping `DB::transaction`
 * rolls the whole thing back — the user is told deletion failed and every
 * row survives, including the ones already deleted inside the transaction.
 *
 * The same shape exists on `gold.campaign_summaries.campaign_id`, which
 * blocks the controller's own explicit `DELETE FROM silver.campaigns`
 * (ProjectController.php:316), so the hand-maintained cleanup list cannot dig
 * itself out of it.
 *
 * Measured on production 2026-08-24: all fourteen tables held zero rows, so
 * the failure is LATENT, not currently firing. It becomes real the first time
 * a project ingests B/S/G drill data. That is also why the one project
 * deleted on 2026-08-24 succeeded — it had no drill data.
 *
 * ## Why catalog-driven, and why ownership-filtered
 *
 * The constraints were created inline and carry auto-generated names
 * (`<table>_<column>_fkey`), which differ between clusters built from
 * migrations and clusters built from `database/raw/`. Naming them explicitly
 * would work on one and silently no-op on the other. Reading pg_constraint
 * fixes whatever the cluster actually has.
 *
 * `pg_has_role(current_user, relowner, 'USAGE')` mirrors
 * 2026_08_24_010000: ALTER TABLE requires ownership, and CI's
 * `migrations-production-privileges` gate deliberately splits ownership
 * between the role that applies `database/raw/` and the role that runs
 * migrations. Without the filter this aborts the whole chain there.
 *
 * The constraint is rebuilt from `conkey`/`confkey` rather than by appending
 * to `pg_get_constraintdef()`: a definition ending in `DEFERRABLE` or
 * `NOT VALID` would make a naive append a syntax error.
 *
 * ## Scope
 *
 * Only FKs whose PARENT is silver.projects, silver.collars or
 * silver.campaigns, and only those currently NO ACTION (`confdeltype = 'a'`).
 * `targeting.target_recommendations.score_id` is deliberately left RESTRICT —
 * it is a scoring lineage pointer, not project-scoped child data, and whether
 * it aborts depends on constraint firing order that cannot be read off the
 * schema. It holds zero rows; revisit it with a real reproduction.
 */
return new class extends Migration
{
    private const PARENTS = ['silver.projects', 'silver.collars', 'silver.campaigns'];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;  // SQLite has no ALTER CONSTRAINT; the test DB stubs these tables without FKs.
        }

        DB::statement(<<<'SQL'
            DO $$
            DECLARE
                r        record;
                child_c  text;
                parent_c text;
                changed  int := 0;
                skipped  text[] := '{}';
            BEGIN
                FOR r IN
                    SELECT con.oid,
                           con.conname,
                           con.conrelid::regclass::text  AS child,
                           con.confrelid::regclass::text AS parent,
                           con.conkey,
                           con.confkey,
                           con.conrelid,
                           con.confrelid,
                           pg_has_role(current_user, cc.relowner, 'USAGE') AS owned
                      FROM pg_constraint con
                      JOIN pg_class     cc ON cc.oid = con.conrelid
                      JOIN pg_class     pc ON pc.oid = con.confrelid
                      JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                     WHERE con.contype = 'f'
                       AND con.confdeltype = 'a'
                       AND pn.nspname || '.' || pc.relname = ANY (
                             ARRAY['silver.projects','silver.collars','silver.campaigns'])
                     ORDER BY 3, 2
                LOOP
                    IF NOT r.owned THEN
                        skipped := skipped || (r.child || '.' || r.conname);
                        CONTINUE;
                    END IF;

                    SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY x.ord)
                      INTO child_c
                      FROM unnest(r.conkey) WITH ORDINALITY AS x(attnum, ord)
                      JOIN pg_attribute a
                        ON a.attrelid = r.conrelid AND a.attnum = x.attnum;

                    SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY x.ord)
                      INTO parent_c
                      FROM unnest(r.confkey) WITH ORDINALITY AS x(attnum, ord)
                      JOIN pg_attribute a
                        ON a.attrelid = r.confrelid AND a.attnum = x.attnum;

                    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.child, r.conname);
                    EXECUTE format(
                        'ALTER TABLE %s ADD CONSTRAINT %I FOREIGN KEY (%s) REFERENCES %s (%s) ON DELETE CASCADE',
                        r.child, r.conname, child_c, r.parent, parent_c);
                    changed := changed + 1;
                END LOOP;

                RAISE NOTICE 'cascade_project_scoped_foreign_keys: % constraint(s) switched to ON DELETE CASCADE', changed;

                IF skipped IS NOT NULL AND cardinality(skipped) > 0 THEN
                    RAISE WARNING
                        'cascade_project_scoped_foreign_keys: skipped % constraint(s) on tables owned by another role: %',
                        cardinality(skipped), array_to_string(skipped, ', ');
                END IF;
            END
            $$;
        SQL);
    }

    public function down(): void
    {
        // Intentionally not reversible. The set this changed is whatever the
        // cluster happened to have as NO ACTION at run time, and restoring it
        // would restore "a child row can silently block a project delete and
        // roll back a partially-completed transaction" — not a state worth
        // going back to. If one specific relationship genuinely needs RESTRICT
        // semantics, add it explicitly in a new migration with the reason.
    }
};
