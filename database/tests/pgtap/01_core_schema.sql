-- pgTAP tests for Module 10 Chunk 10.3 — core schema coverage (migrations 01-07)
-- File: database/tests/pgtap/01_core_schema.sql
--
-- Run: ./database/tests/pgtap/run.sh --filter 01
-- Requires: pgTAP extension installed in the georag database.
--
-- Closes audit finding M-A5-05: "only migrations 08–11 have pgTAP coverage".
--
-- Coverage:
--   • silver.workspaces (migration 2026_04_20_100000)
--   • silver.projects   (migration 2026_04_09_180000)
--   • public.users      (migration 0001_01_01_000000 — Laravel default)
--   • project_user pivot (migration 2026_04_11_000000)
--
-- Assertions (plan: 10):
--   1.  silver.workspaces exists
--   2.  silver.workspaces.workspace_id column exists (UUID PK)
--   3.  silver.workspaces.workspace_id is NOT NULL
--   4.  silver.workspaces.name column exists
--   5.  silver.projects exists
--   6.  silver.projects.project_id column exists (UUID PK)
--   7.  silver.projects.workspace_id column exists (FK to workspaces)
--   8.  users table exists (public schema, Laravel-managed)
--   9.  users.email column exists
--   10. project_user pivot table exists with role column

BEGIN;

SELECT plan(10);

-- ── 1. silver.workspaces exists ─────────────────────────────────────────────
SELECT has_table(
    'silver', 'workspaces',
    'silver.workspaces table exists'
);

-- ── 2. silver.workspaces.workspace_id column exists ─────────────────────────
SELECT has_column(
    'silver', 'workspaces', 'workspace_id',
    'silver.workspaces has workspace_id column'
);

-- ── 3. workspace_id is NOT NULL ──────────────────────────────────────────────
SELECT col_not_null(
    'silver', 'workspaces', 'workspace_id',
    'silver.workspaces.workspace_id is NOT NULL'
);

-- ── 4. silver.workspaces.name column exists ──────────────────────────────────
SELECT has_column(
    'silver', 'workspaces', 'name',
    'silver.workspaces has name column'
);

-- ── 5. silver.projects exists ───────────────────────────────────────────────
SELECT has_table(
    'silver', 'projects',
    'silver.projects table exists'
);

-- ── 6. silver.projects.project_id column exists ─────────────────────────────
SELECT has_column(
    'silver', 'projects', 'project_id',
    'silver.projects has project_id column'
);

-- ── 7. silver.projects.workspace_id column exists (FK added in migration
--        2026_04_20_100000 — workspace bootstrap) ─────────────────────────────
SELECT has_column(
    'silver', 'projects', 'workspace_id',
    'silver.projects has workspace_id column (FK to workspaces)'
);

-- ── 8. public.users table exists (Laravel 0001_01_01_000000) ────────────────
SELECT has_table(
    'public', 'users',
    'public.users table exists'
);

-- ── 9. users.email column exists ────────────────────────────────────────────
SELECT has_column(
    'public', 'users', 'email',
    'public.users has email column'
);

-- ── 10. project_user pivot table exists with role column ────────────────────
SELECT ok(
    (
        SELECT COUNT(*) = 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name   = 'project_user'
           AND column_name  = 'role'
    ),
    'project_user pivot has role column'
);

SELECT * FROM finish();
ROLLBACK;
