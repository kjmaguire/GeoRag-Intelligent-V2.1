-- =============================================================================
-- Follow-up to 111-fix-rls-nullif-guard.sql.
--
-- 111 wrapped every current_setting() call in NULLIF, which fixed the
-- "invalid input syntax for type uuid: \"\"" crash. But the policy
-- semantics still require a non-NULL workspace_id to match — which
-- means Laravel queries that never set `app.workspace_id` (Sanctum API,
-- dashboard controllers) see ZERO rows. That broke /api/v1/projects/
-- {id}/collars (and similar) for the customer-facing API.
--
-- Fix: for every policy that filters by a workspace/project setting,
-- prepend `NULLIF(...) IS NULL OR` so unset-GUC requests see everything,
-- and set-GUC requests see only their scoped subset.
--
-- This matches the pattern §29.3 tenant isolation policies use elsewhere.
--
-- Idempotent — re-runs are no-ops because policies already include the
-- "IS NULL OR" prefix after this migration.
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
         WHERE pg_get_expr(pp.polqual, pp.polrelid) LIKE '%NULLIF(current_setting%'
           AND pg_get_expr(pp.polqual, pp.polrelid) NOT LIKE '%IS NULL OR%'
    LOOP
        -- Wrap the existing qual with "NULLIF(...) IS NULL OR (existing)"
        -- using the FIRST current_setting reference's setting name.
        DECLARE
            setting_name text;
        BEGIN
            setting_name := substring(rec.qual FROM 'current_setting\(([^,)]+)');
            IF setting_name IS NULL THEN
                CONTINUE;
            END IF;

            new_qual := format(
                '(NULLIF(current_setting(%s, true), '''') IS NULL) OR (%s)',
                setting_name, rec.qual
            );
            new_chk := CASE
                WHEN rec.chk IS NULL THEN NULL
                ELSE format(
                    '(NULLIF(current_setting(%s, true), '''') IS NULL) OR (%s)',
                    setting_name, rec.chk
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
        END;
    END LOOP;
END $$;

-- Verify
DO $$
DECLARE
    fixed int; missing int;
BEGIN
    SELECT count(*) INTO fixed
      FROM pg_policy pp
     WHERE pg_get_expr(pp.polqual, pp.polrelid) LIKE '%IS NULL OR%';
    SELECT count(*) INTO missing
      FROM pg_policy pp
     WHERE pg_get_expr(pp.polqual, pp.polrelid) LIKE '%NULLIF(current_setting%'
       AND pg_get_expr(pp.polqual, pp.polrelid) NOT LIKE '%IS NULL OR%';
    RAISE NOTICE 'RLS permissive branch: % policies now safe, % still strict', fixed, missing;
END $$;
