-- pgTAP tests for the 2026-10 database review migrations
-- File: database/tests/pgtap/13_database_review_2026_10.sql
--
-- Run: ./database/tests/pgtap/run.sh --filter 13
-- Requires: pgTAP extension installed in the georag database.
--
-- Covers:
--   2026_10_06_100000  FK indexes for the project-delete path; duplicate
--                      workspace_id indexes removed
--   2026_10_06_100100  silver.samples.workspace_id NOT NULL + FK + autopopulation
--   2026_10_06_100200  martin_readonly cut back to the tile sources' relations
--
-- Assertions (plan: 14):
--   1-4.   project/score FK columns are indexed
--   5.     no identical duplicate indexes on the four formerly-duplicated tables
--   6-8.   silver.samples: workspace_id NOT NULL, FK to silver.workspaces,
--          autopopulation trigger present
--   9-10.  martin_readonly keeps SELECT on a tile source (collars) and on
--          gold.cross_section_panels
--   11-12. martin_readonly has NO SELECT on silver.answer_runs / qp_credentials
--   13.    no default privilege hands martin_readonly SELECT on future silver tables
--   14.    silver.significant_intersections_by_project is not executable by PUBLIC

BEGIN;

SELECT plan(14);

-- ── 1-4. FK columns indexed ─────────────────────────────────────────────────

SELECT has_index('silver', 'campaigns', 'idx_campaigns_project_id', 'project_id',
    'silver.campaigns.project_id is indexed (project delete RI check)');
SELECT has_index('gold', 'zone_statistics', 'idx_zone_statistics_project_id', 'project_id',
    'gold.zone_statistics.project_id is indexed (project delete RI check)');
SELECT has_index('gold', 'element_correlations', 'idx_element_correlations_project_id', 'project_id',
    'gold.element_correlations.project_id is indexed (project delete RI check)');
SELECT has_index('targeting', 'target_recommendations', 'idx_target_recommendations_score_id', 'score_id',
    'targeting.target_recommendations.score_id is indexed (ON DELETE RESTRICT RI check)');

-- ── 5. no identical duplicate indexes on the four formerly-duplicated tables ─
-- Migration-only clusters carry one index here; clusters where db:apply-raw
-- has run carried two (idx_<t>_workspace + idx_<t>_workspace_id) until
-- 2026_10_06_100000. Either way the end state must be a single index.

SELECT is(
    (SELECT count(*)::int
       FROM (
            SELECT indrelid
              FROM pg_index
             WHERE indrelid IN ('silver.drill_traces'::regclass,
                                'gold.drillhole_intervals_visual'::regclass,
                                'gold.cross_section_panels'::regclass,
                                'gold.structure_measurements_visual'::regclass)
               AND indisvalid
             GROUP BY indrelid, indkey::text, indclass::text, indoption::text,
                      COALESCE(pg_get_expr(indpred, indrelid), ''),
                      COALESCE(pg_get_expr(indexprs, indrelid), '')
            HAVING count(*) > 1
       ) d),
    0,
    'silver.drill_traces and the three gold visual tables carry no identical duplicate indexes'
);

-- ── 6-8. silver.samples tenant column ───────────────────────────────────────

SELECT col_not_null('silver', 'samples', 'workspace_id',
    'silver.samples.workspace_id is NOT NULL');

SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'silver.samples'::regclass
           AND contype = 'f'
           AND confrelid = 'silver.workspaces'::regclass
           AND confdeltype = 'c'
    ),
    'silver.samples.workspace_id references silver.workspaces ON DELETE CASCADE'
);

SELECT ok(
    EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid = 'silver.samples'::regclass
           AND tgname = 'trg_samples_default_workspace_id'
           AND NOT tgisinternal
    ),
    'silver.samples has the workspace_id autopopulation trigger'
);

-- ── 9-12. martin_readonly least privilege ───────────────────────────────────

SELECT ok(
    has_table_privilege('martin_readonly', 'silver.collars', 'SELECT'),
    'martin_readonly can still SELECT silver.collars (tile source)'
);

SELECT ok(
    has_table_privilege('martin_readonly', 'gold.cross_section_panels', 'SELECT'),
    'martin_readonly can still SELECT gold.cross_section_panels (tile source)'
);

SELECT ok(
    NOT has_table_privilege('martin_readonly', 'silver.answer_runs', 'SELECT'),
    'martin_readonly cannot SELECT silver.answer_runs (chat history)'
);

SELECT ok(
    NOT has_table_privilege('martin_readonly', 'silver.qp_credentials', 'SELECT'),
    'martin_readonly cannot SELECT silver.qp_credentials'
);

-- ── 13. no default privilege for future silver tables ───────────────────────

SELECT ok(
    NOT EXISTS (
        SELECT 1
          FROM pg_default_acl d
          CROSS JOIN LATERAL aclexplode(d.defaclacl) a
         WHERE d.defaclnamespace = 'silver'::regnamespace
           AND d.defaclobjtype = 'r'
           AND a.grantee = 'martin_readonly'::regrole
    ),
    'no default privilege grants martin_readonly SELECT on future silver tables'
);

-- ── 14. SECURITY DEFINER tile function not executable by PUBLIC ─────────────

SELECT ok(
    NOT EXISTS (
        SELECT 1
          FROM pg_proc p
          CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a
         WHERE p.oid = 'silver.significant_intersections_by_project(integer, integer, integer, json)'::regprocedure
           AND a.grantee = 0
           AND a.privilege_type = 'EXECUTE'
    ),
    'silver.significant_intersections_by_project is not executable by PUBLIC'
);

SELECT * FROM finish();
ROLLBACK;
