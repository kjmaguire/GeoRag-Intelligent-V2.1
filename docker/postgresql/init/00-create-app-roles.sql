-- 00-create-app-roles.sql
--
-- Runtime application roles. Runs before init-roles.sql (file ordering
-- by leading numeric prefix: 00- → 10- → 20- → init-roles.sql).
--
-- Why this file exists
-- --------------------
-- `database/raw/phase1/10-georag-app-role.sql` is the canonical
-- definition of georag_app, but that path is NOT mounted into the
-- postgres container's /docker-entrypoint-initdb.d. On a fresh cluster
-- bootstrap, init-roles.sql (which only creates georag_read/_write/
-- _audit) runs without georag_app present, and every migration that
-- GRANTs to georag_app then fails. Same story for martin_ro (Martin
-- tile server reader) — it lives only in the migrations and the role
-- was never created at init time. See the P1-A audit finding in
-- docs/handover/AUDIT_AND_FIX_REPORT.md.
--
-- georag_app  : FastAPI + PgBouncer pooled connections (LOGIN, INHERIT).
-- martin_ro   : Martin tile server (LOGIN, INHERIT).
--
-- INHERIT note (2026-06-03 audit item E): SAD §4.2 originally specified
-- martin_ro as NOINHERIT, with Martin expected to SET ROLE georag_read
-- before each tile query. That's overengineered defense-in-depth for a
-- role whose only granted privileges are SELECT-on-silver (via
-- georag_read membership) + EXECUTE-on-pg_*_by_project (per
-- 2026_06_03_030000 migration). The security goal — Martin can't write
-- to silver — is achieved by NOT granting georag_write membership, not
-- by the INHERIT flag. INHERIT removes per-query SET ROLE overhead
-- without weakening the threat model.
--
-- Passwords are seeded as placeholders here. Production deploys must
-- rotate them to SOPS-managed values BEFORE the cluster handles any
-- real traffic — leaving the placeholder password live is equivalent
-- to a public credential. See docs/RUNBOOK.md secret-management.
-- Ordering note: this file runs at the START of bootstrap. The companion
-- file 90-grant-app-role-memberships.sql runs AFTER init-roles.sql and
-- does the GRANT georag_read/_write TO georag_app. We split the work so
-- the membership grants don't silently no-op when init-roles.sql hasn't
-- yet created georag_read/_write (alphabetical file ordering puts
-- init-*.sql after 00-/10-/20-/Z_*).
DO $$
BEGIN

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
    CREATE ROLE georag_app
      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE INHERIT LOGIN
      PASSWORD 'change_in_production';
    RAISE NOTICE '00-create-app-roles: created georag_app';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'martin_ro') THEN
    CREATE ROLE martin_ro
      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE INHERIT LOGIN
      PASSWORD 'change_in_production';
    RAISE NOTICE '00-create-app-roles: created martin_ro';
  END IF;

  -- Idempotent re-assert: if an earlier bootstrap created martin_ro as
  -- NOINHERIT (audit item E shipped 2026-06-03), normalize to INHERIT.
  -- Safe: martin_ro's only privileges are SELECT-shaped (via georag_read
  -- membership + EXECUTE on tile functions); INHERIT just removes the
  -- per-query SET ROLE Martin would otherwise need.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'martin_ro' AND NOT rolinherit) THEN
    ALTER ROLE martin_ro INHERIT;
    RAISE NOTICE '00-create-app-roles: normalized martin_ro to INHERIT';
  END IF;

  -- Defense-in-depth: hard-fail bootstrap if either role accidentally
  -- inherited SUPERUSER or BYPASSRLS. RLS is the entire workspace
  -- isolation story; a bypass on these roles silently defeats it.
  IF (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = 'georag_app') THEN
    RAISE EXCEPTION '00-create-app-roles: georag_app has SUPERUSER or BYPASSRLS — refusing to continue';
  END IF;
  IF (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = 'martin_ro') THEN
    RAISE EXCEPTION '00-create-app-roles: martin_ro has SUPERUSER or BYPASSRLS — refusing to continue';
  END IF;

END $$;
