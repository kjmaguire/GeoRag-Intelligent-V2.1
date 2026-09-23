-- Remove exactly what seed_multitenant_corpus.sql created, nothing else.
--
-- Deletes by the deterministic ids the seed uses rather than by a `LIKE
-- 'rehearsal-%'` sweep: a slug pattern would also match anything a colleague
-- happened to name that way, and this runs against a live database.
BEGIN;

DELETE FROM silver.collars WHERE collar_id IN (
    'c0000001-aaaa-4aaa-8aaa-000000000001',
    'c0000002-bbbb-4bbb-8bbb-000000000002',
    'c0000003-aaaa-4aaa-8aaa-000000000003',
    'c0000004-bbbb-4bbb-8bbb-000000000004',
    'c0000005-aaaa-4aaa-8aaa-000000000005',
    'c0000006-bbbb-4bbb-8bbb-000000000006'
);

DELETE FROM silver.projects WHERE project_id IN (
    'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
    'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb'
);

DELETE FROM silver.workspaces WHERE workspace_id IN (
    '11111111-aaaa-4aaa-8aaa-111111111111',
    '22222222-bbbb-4bbb-8bbb-222222222222'
);

COMMIT;

-- No psql meta-commands, and the readback ASSERTS rather than prints: this
-- file also runs over asyncpg (see verify_tenant_fence.sql's header). A
-- teardown that reports leftovers in a table nobody reads is a teardown that
-- silently half-worked.
DO $$
DECLARE
    n_ws int; n_pr int; n_co int;
BEGIN
    SELECT count(*) INTO n_ws FROM silver.workspaces
     WHERE workspace_id IN ('11111111-aaaa-4aaa-8aaa-111111111111',
                            '22222222-bbbb-4bbb-8bbb-222222222222');
    SELECT count(*) INTO n_pr FROM silver.projects
     WHERE project_id IN ('aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
                          'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb');
    SELECT count(*) INTO n_co FROM silver.collars
     WHERE workspace_id IN ('11111111-aaaa-4aaa-8aaa-111111111111',
                            '22222222-bbbb-4bbb-8bbb-222222222222');

    IF n_ws + n_pr + n_co <> 0 THEN
        RAISE EXCEPTION
            'teardown left rows behind: % workspaces, % projects, % collars', n_ws, n_pr, n_co;
    END IF;
    RAISE NOTICE '[TEARDOWN-OK] rehearsal corpus removed; 0 workspaces, 0 projects, 0 collars remain';
END $$;
