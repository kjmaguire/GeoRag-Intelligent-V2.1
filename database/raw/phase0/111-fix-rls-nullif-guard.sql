-- =============================================================================
-- Bulk-patch every RLS policy that uses current_setting() without a NULLIF
-- guard. Without the guard, any query that doesn't pre-set the GUC blows up
-- with `invalid input syntax for type uuid: ""` (empty-string cast to uuid).
--
-- This is the same root cause as the silver.projects fix shipped earlier;
-- this migration sweeps all 78 affected policies in one pass.
--
-- Strategy:
--   For each policy whose qual/withcheck contains `current_setting(...)` but
--   NOT `NULLIF`, drop + recreate with the same expression text but every
--   `current_setting(NAME, true)` wrapped as
--   `NULLIF(current_setting(NAME, true), '')`.
--
-- Idempotent — re-runs are no-ops because patched policies now contain NULLIF.
-- =============================================================================

DO $$
DECLARE
    rec record;
    new_qual text;
    new_chk text;
    sql text;
BEGIN
    FOR rec IN
        SELECT pn.nspname AS schema_name,
               pc.relname AS table_name,
               pp.polname AS policy_name,
               pg_get_expr(pp.polqual, pp.polrelid) AS qual,
               pg_get_expr(pp.polwithcheck, pp.polrelid) AS chk,
               pp.polcmd AS cmd
          FROM pg_policy pp
          JOIN pg_class pc ON pc.oid = pp.polrelid
          JOIN pg_namespace pn ON pn.oid = pc.relnamespace
         WHERE pg_get_expr(pp.polqual, pp.polrelid) LIKE '%current_setting%'
           AND pg_get_expr(pp.polqual, pp.polrelid) NOT LIKE '%NULLIF%'
    LOOP
        new_qual := regexp_replace(
            rec.qual,
            'current_setting\(([^)]+)\)',
            'NULLIF(current_setting(\1), '''')',
            'g'
        );
        new_chk := CASE
            WHEN rec.chk IS NULL THEN NULL
            ELSE regexp_replace(
                rec.chk,
                'current_setting\(([^)]+)\)',
                'NULLIF(current_setting(\1), '''')',
                'g'
            )
        END;

        EXECUTE format('DROP POLICY IF EXISTS %I ON %I.%I',
                       rec.policy_name, rec.schema_name, rec.table_name);

        sql := format('CREATE POLICY %I ON %I.%I',
                      rec.policy_name, rec.schema_name, rec.table_name);
        IF rec.cmd = 'r' THEN sql := sql || ' FOR SELECT';
        ELSIF rec.cmd = 'a' THEN sql := sql || ' FOR INSERT';
        ELSIF rec.cmd = 'w' THEN sql := sql || ' FOR UPDATE';
        ELSIF rec.cmd = 'd' THEN sql := sql || ' FOR DELETE';
        END IF;

        sql := sql || ' USING (' || new_qual || ')';
        IF new_chk IS NOT NULL THEN
            sql := sql || ' WITH CHECK (' || new_chk || ')';
        END IF;

        BEGIN
            EXECUTE sql;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'Failed to patch policy %.%.%: %',
                rec.schema_name, rec.table_name, rec.policy_name, SQLERRM;
        END;
    END LOOP;
END $$;

-- Verify
DO $$
DECLARE
    remaining int;
BEGIN
    SELECT count(*) INTO remaining
      FROM pg_policy pp
     WHERE pg_get_expr(pp.polqual, pp.polrelid) LIKE '%current_setting%'
       AND pg_get_expr(pp.polqual, pp.polrelid) NOT LIKE '%NULLIF%';
    RAISE NOTICE 'RLS NULLIF guard sweep: % policies still broken', remaining;
END $$;
