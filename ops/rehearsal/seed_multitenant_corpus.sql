-- Two-tenant rehearsal corpus for the live AWS deployment.
--
-- WHY THIS EXISTS, and why the coordinates below are not arbitrary
-- ----------------------------------------------------------------
-- database/tests/pgtap/08_silver_mvt_functions.sql tests 73-77 already prove
-- the Martin tenant fence is CORRECT: cross-tenant pairing denies, the
-- matching pairs return data (both positive controls present, so the denial
-- is non-vacuous), and missing/malformed workspace_id raises. That runs in
-- CI against a real Postgres on every PR.
--
-- This file answers a different question, and only a live deployment can:
-- does the fence hold on the RDS instance this platform actually runs on,
-- with workspaces created the way real ones are? CI proves the function
-- body. This proves the function body that is DEPLOYED, against rows that
-- went in through the same schema production uses.
--
-- CO-LOCATION IS THE WHOLE DESIGN. The two tenants' collars are interleaved
-- 100 m apart along one line, not clustered in separate regions. Give tenant
-- A collars in British Columbia and tenant B collars in Saskatchewan and
-- every cross-tenant tile assertion passes whether or not the fence works,
-- because the bounding box already excluded the other tenant's rows. The
-- test would be vacuous and would look exactly like a passing one. Here,
-- ANY tile that contains one tenant's collar contains the other's, so the
-- workspace_id predicate is the only thing that can separate them.
--
-- Idempotent: safe to re-run. Every id is deterministic so the verification
-- script can reference rows by name rather than discovering them.
--
-- Teardown: ops/rehearsal/teardown_multitenant_corpus.sql

BEGIN;

-- ── Tenants ──────────────────────────────────────────────────────────────
INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
VALUES
    ('11111111-aaaa-4aaa-8aaa-111111111111', 'Meridian Exploration (rehearsal)', 'rehearsal-meridian', NOW(), NOW()),
    ('22222222-bbbb-4bbb-8bbb-222222222222', 'Cascade Minerals (rehearsal)',     'rehearsal-cascade',  NOW(), NOW())
ON CONFLICT (workspace_id) DO UPDATE SET name = EXCLUDED.name, updated_at = NOW();

-- ── One project each ─────────────────────────────────────────────────────
INSERT INTO silver.projects (
    project_id, project_name, crs_datum, orientation_reference,
    status, slug, workspace_id, data_version
) VALUES
    ('aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa', 'Meridian — Kesler Creek', 'EPSG:32613', 'magnetic',
     'active', 'rehearsal-meridian-kesler', '11111111-aaaa-4aaa-8aaa-111111111111', 1),
    ('bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb', 'Cascade — Kesler Creek',  'EPSG:32613', 'magnetic',
     'active', 'rehearsal-cascade-kesler',  '22222222-bbbb-4bbb-8bbb-222222222222', 1)
ON CONFLICT (project_id) DO UPDATE SET data_version = 1;

-- ── Collars, INTERLEAVED ─────────────────────────────────────────────────
-- Same northing, eastings 100 m apart, alternating tenant. Deliberately the
-- same deposit name in both projects too: if a query or tile ever returns a
-- MER- hole to Cascade or a CAS- hole to Meridian, the prefix names the leak
-- immediately rather than leaving you to diff UUIDs.
INSERT INTO silver.collars (
    collar_id, hole_id, project_id, workspace_id,
    easting, northing, elevation, total_depth, hole_type, azimuth, dip, status,
    geom, created_at, updated_at
) VALUES
    ('c0000001-aaaa-4aaa-8aaa-000000000001', 'MER-001', 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa', '11111111-aaaa-4aaa-8aaa-111111111111',
     500000, 5900000, 1010, 152.4, 'DD', 45,  -60, 'completed',
     ST_SetSRID(ST_MakePoint(500000, 5900000), 32613), NOW(), NOW()),
    ('c0000002-bbbb-4bbb-8bbb-000000000002', 'CAS-001', 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb', '22222222-bbbb-4bbb-8bbb-222222222222',
     500100, 5900000, 1012, 168.0, 'DD', 90,  -55, 'completed',
     ST_SetSRID(ST_MakePoint(500100, 5900000), 32613), NOW(), NOW()),
    ('c0000003-aaaa-4aaa-8aaa-000000000003', 'MER-002', 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa', '11111111-aaaa-4aaa-8aaa-111111111111',
     500200, 5900000, 1008, 201.5, 'DD', 135, -70, 'completed',
     ST_SetSRID(ST_MakePoint(500200, 5900000), 32613), NOW(), NOW()),
    ('c0000004-bbbb-4bbb-8bbb-000000000004', 'CAS-002', 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb', '22222222-bbbb-4bbb-8bbb-222222222222',
     500300, 5900000, 1015, 96.0,  'RC', 180, -50, 'completed',
     ST_SetSRID(ST_MakePoint(500300, 5900000), 32613), NOW(), NOW()),
    ('c0000005-aaaa-4aaa-8aaa-000000000005', 'MER-003', 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa', '11111111-aaaa-4aaa-8aaa-111111111111',
     500400, 5900000, 1004, 143.2, 'RC', 225, -65, 'completed',
     ST_SetSRID(ST_MakePoint(500400, 5900000), 32613), NOW(), NOW()),
    ('c0000006-bbbb-4bbb-8bbb-000000000006', 'CAS-003', 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb', '22222222-bbbb-4bbb-8bbb-222222222222',
     500500, 5900000, 1018, 187.6, 'DD', 270, -80, 'completed',
     ST_SetSRID(ST_MakePoint(500500, 5900000), 32613), NOW(), NOW())
ON CONFLICT (collar_id) DO UPDATE SET
    easting = EXCLUDED.easting, northing = EXCLUDED.northing, geom = EXCLUDED.geom,
    updated_at = NOW();

COMMIT;

\echo ''
\echo 'Seeded. Tenants, projects and interleaved collars:'
SELECT w.slug AS workspace, p.slug AS project, c.hole_id,
       c.easting, c.northing
FROM silver.collars c
JOIN silver.projects p   ON p.project_id   = c.project_id
JOIN silver.workspaces w ON w.workspace_id = p.workspace_id
WHERE w.slug IN ('rehearsal-meridian', 'rehearsal-cascade')
ORDER BY c.easting;
