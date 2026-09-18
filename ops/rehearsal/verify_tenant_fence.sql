-- Martin tenant fence, verified against the LIVE deployment.
--
-- WHAT THIS ADDS OVER CI. database/tests/pgtap/08_silver_mvt_functions.sql
-- tests 73-77 already prove the fence logic is correct, and run on every PR.
-- They cannot prove two things that only a real deployment can:
--
--   1. That the RDS instance this platform runs on actually CARRIES those
--      function definitions — i.e. that 2026_09_16_120000 was applied here
--      and not just merged.
--   2. That it holds for workspaces created the ordinary way, rather than
--      for a fixture built by the same commit as the function.
--
-- Plain SQL, not pgTAP: pgTAP is a CI convenience and is not installed on
-- RDS. Every check RAISEs on failure, so a non-zero psql exit is the verdict
-- and a silent pass is impossible.
--
-- Run ops/rehearsal/seed_multitenant_corpus.sql first.

\set ON_ERROR_STOP on

\echo '== 0. precondition: the fence migration is actually deployed here =='
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc pr
        JOIN pg_namespace n ON n.oid = pr.pronamespace
        WHERE n.nspname = 'silver' AND pr.proname = 'pg_collars_by_project'
    ) THEN
        RAISE EXCEPTION 'silver.pg_collars_by_project does not exist on this database';
    END IF;

    -- The pre-fence version took no workspace_id and would answer happily.
    -- Reaching for the description rather than parsing the body: the
    -- migration sets it explicitly and it is the cheapest honest signal that
    -- THIS deployment has the post-fix definition.
    IF (SELECT obj_description(pr.oid, 'pg_proc')
          FROM pg_proc pr
          JOIN pg_namespace n ON n.oid = pr.pronamespace
         WHERE n.nspname = 'silver' AND pr.proname = 'pg_collars_by_project'
         LIMIT 1) NOT LIKE '%Requires workspace_id%'
    THEN
        RAISE EXCEPTION
            'silver.pg_collars_by_project exists but is the PRE-FENCE definition — '
            'migration 2026_09_16_120000 has not been applied to this database';
    END IF;
END $$;
\echo '   ok: post-fence definition present'

\echo '== 1. precondition: the two tenants are CO-LOCATED =='
-- Without this the cross-tenant assertions below are vacuous: if the tenants
-- sit in different regions, a tile scoped to one simply cannot contain the
-- other's rows and every denial passes for the wrong reason. Asserted rather
-- than assumed, so a later edit to the seed coordinates breaks loudly here
-- instead of quietly turning section 3 into a no-op.
DO $$
DECLARE
    n_shared int;
BEGIN
    SELECT count(*) INTO n_shared
    FROM silver.collars a
    JOIN silver.collars b ON a.workspace_id <> b.workspace_id
    WHERE a.workspace_id = '11111111-aaaa-4aaa-8aaa-111111111111'
      AND b.workspace_id = '22222222-bbbb-4bbb-8bbb-222222222222'
      AND ST_DWithin(ST_Transform(a.geom, 3857), ST_Transform(b.geom, 3857), 2000);

    IF n_shared = 0 THEN
        RAISE EXCEPTION
            'the two rehearsal tenants are not co-located — every cross-tenant '
            'assertion below would pass vacuously. Re-seed with interleaved coordinates.';
    END IF;
    RAISE NOTICE '   ok: % co-located cross-tenant collar pairs within 2 km', n_shared;
END $$;

\echo '== 2. positive controls: each tenant sees its own data =='
DO $$
DECLARE
    mvt_a bytea;
    mvt_b bytea;
BEGIN
    SELECT mvt INTO mvt_a FROM silver.pg_collars_by_project(1, 0, 0,
        '{"project_id":"aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
          "workspace_id":"11111111-aaaa-4aaa-8aaa-111111111111"}'::json);
    IF mvt_a IS NULL THEN
        RAISE EXCEPTION 'Meridian cannot see its OWN collars — the fence is over-filtering';
    END IF;

    SELECT mvt INTO mvt_b FROM silver.pg_collars_by_project(1, 0, 0,
        '{"project_id":"bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
          "workspace_id":"22222222-bbbb-4bbb-8bbb-222222222222"}'::json);
    IF mvt_b IS NULL THEN
        RAISE EXCEPTION 'Cascade cannot see its OWN collars — the fence is over-filtering';
    END IF;
    RAISE NOTICE '   ok: both tenants see their own tiles (% / % bytes)',
        octet_length(mvt_a), octet_length(mvt_b);
END $$;

\echo '== 3. cross-tenant denial, BOTH directions =='
-- pgTAP test 74 covers one direction only (B's id against A's project). A
-- fence that were asymmetric for any reason would pass there and leak here,
-- so both orderings are asserted.
--
-- The fence has TWO independent guards and this distinguishes them, because
-- they fail differently and only one of them discloses anything:
--
--   * `AND p.workspace_id = v_wsid` on the silver.projects lookup makes a
--     foreign pairing resolve to "project not found" — the function returns
--     NO ROW at all, so mvt is NULL.
--   * `AND c.workspace_id = v_wsid` on the collar scan filters the features
--     themselves.
--
-- Remove only the first and you get a non-NULL but ZERO-BYTE tile: the guard
-- is gone, yet the second predicate still withheld every feature, so nothing
-- actually leaked. Remove both and the tile carries the other tenant's holes.
-- Reporting those two as the same "LEAK" would overstate the first and
-- understate the second, so the byte count decides the wording.
DO $$
DECLARE
    leaked bytea;
    n      int;
BEGIN
    FOR n IN 1..2 LOOP
        IF n = 1 THEN
            SELECT mvt INTO leaked FROM silver.pg_collars_by_project(1, 0, 0,
                '{"project_id":"aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
                  "workspace_id":"22222222-bbbb-4bbb-8bbb-222222222222"}'::json);
        ELSE
            SELECT mvt INTO leaked FROM silver.pg_collars_by_project(1, 0, 0,
                '{"project_id":"bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb",
                  "workspace_id":"11111111-aaaa-4aaa-8aaa-111111111111"}'::json);
        END IF;

        IF leaked IS NOT NULL AND octet_length(leaked) > 0 THEN
            RAISE EXCEPTION
                'DISCLOSURE (direction %): a cross-tenant pairing returned % bytes of '
                'another tenant''s features', n, octet_length(leaked);
        ELSIF leaked IS NOT NULL THEN
            RAISE EXCEPTION
                'FENCE BYPASS (direction %): a cross-tenant pairing returned a tile '
                '(0 bytes). No features escaped — the collar-level workspace_id '
                'predicate still held — but the silver.projects guard did not, and it '
                'is the only thing standing between a foreign project_id and its rows '
                'on any table whose own workspace_id is NULL.', n;
        END IF;
    END LOOP;
    RAISE NOTICE '   ok: cross-tenant pairing denied in both directions';
END $$;

\echo '== 4. a missing or malformed workspace_id RAISES, not blank-tiles =='
-- A blank tile is indistinguishable from "no data here", which is how a
-- filtering failure hides in plain sight on a map.
DO $$
DECLARE
    dummy bytea;
BEGIN
    BEGIN
        SELECT mvt INTO dummy FROM silver.pg_collars_by_project(1, 0, 0,
            '{"project_id":"aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"}'::json);
        RAISE EXCEPTION 'omitting workspace_id did NOT raise — it returned a tile instead';
    EXCEPTION WHEN sqlstate 'P0001' THEN
        IF SQLERRM LIKE '%did NOT raise%' THEN RAISE; END IF;
    END;

    BEGIN
        SELECT mvt INTO dummy FROM silver.pg_collars_by_project(1, 0, 0,
            '{"project_id":"aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
              "workspace_id":"not-a-uuid"}'::json);
        RAISE EXCEPTION 'a malformed workspace_id did NOT raise';
    EXCEPTION WHEN sqlstate 'P0001' THEN
        IF SQLERRM LIKE '%did NOT raise%' THEN RAISE; END IF;
    END;
    RAISE NOTICE '   ok: missing and malformed workspace_id both raise';
END $$;

\echo ''
\echo 'TENANT FENCE VERIFIED on this database.'
