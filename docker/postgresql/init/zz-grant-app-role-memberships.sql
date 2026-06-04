-- zz-grant-app-role-memberships.sql
--
-- Companion to 00-create-app-roles.sql. The `zz-` lowercase prefix
-- guarantees this runs AFTER `init-roles.sql` and every other init
-- file. ASCII collation orders digits < uppercase < lowercase, so a
-- numeric prefix like `90-` (which audit pass 3 originally drafted)
-- still sorts BEFORE the lowercase `init-*` files — exactly the same
-- bug as the original 00- attempt that this file fixes. Use
-- `ls -1 docker/postgresql/init/ | sort` to verify the run order
-- before renaming.
--
-- By the time this runs:
--   * 00-create-app-roles.sql has created georag_app + martin_ro
--   * init-roles.sql has created georag_read + georag_write + georag_audit
-- so the role membership grants below resolve cleanly. The 00- file
-- intentionally does NOT do these grants — at that point georag_read
-- and georag_write don't yet exist, and the IF EXISTS guard would
-- silently skip them, leaving georag_app with zero inherited
-- privileges (the original bug found in audit pass 3).
--
-- All grants are idempotent: GRANT role TO role re-issued against an
-- existing membership is a no-op.
DO $$
BEGIN

  -- georag_app inherits read+write privileges via role membership.
  -- INHERIT is set on the role itself (00-create-app-roles.sql) so the
  -- application's pooled connections automatically pick up granted
  -- privileges without needing SET ROLE.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app')
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_read') THEN
    GRANT georag_read TO georag_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app')
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_write') THEN
    GRANT georag_write TO georag_app;
  END IF;

  -- martin_ro inherits georag_read so SELECT-on-silver flows through
  -- without per-query SET ROLE. INHERIT was flipped 2026-06-03 per
  -- audit item E — see 00-create-app-roles.sql for the rationale.
  -- Tile-function EXECUTE grants live in their own migration
  -- (2026_06_03_030000_grant_tile_functions_to_martin_ro).
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'martin_ro')
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_read') THEN
    GRANT georag_read TO martin_ro;
  END IF;

END $$;
