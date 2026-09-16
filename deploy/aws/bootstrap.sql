-- One-time bootstrap for a fresh RDS instance (ADR-0022).
--
-- WHY THIS FILE EXISTS
--
-- `docker/postgresql/init/*.sql` runs from /docker-entrypoint-initdb.d/ on a
-- fresh compose volume and nowhere else. Ch 02 §1.2 records that those
-- scripts "never run on Azure", because Flexible Server was provisioned by
-- hand — and they will not run on RDS either. Everything they create that
-- neither `php artisan migrate` nor `php artisan db:apply-raw` creates has
-- to be applied once, by hand, as the master user.
--
-- Getting this wrong does not fail loudly. The extensions are the visible
-- half; the roles are the half that produces a working-looking deployment
-- where the Hatchet engine cannot reach its own database and the grant
-- chains in init-roles.sql simply do not exist.
--
-- WHAT IS **NOT** HERE, BECAUSE SOMETHING ELSE DOES IT
--
--   georag_app       GRANTS only -- migration 2026_08_19_050000 grants it
--                    per-object privileges on silver/bronze/gold and sets
--                    default privileges. The ROLE itself is created here;
--                    see the block below for why it had to move.
--   martin_readonly  the migration creates it, but NOLOGIN -- and the martin
--                    task connects as it, so this file creates it first WITH
--                    login. See the block below for why that is here and not
--                    in the migration.
--   every schema     the migration chain
--   RLS policies     the phase0 files in database/raw/manifest.json
--
-- RUN AS THE MASTER USER, ONCE, BEFORE THE FIRST DEPLOY:
--
--   psql "$(terraform -chdir=deploy/aws/terraform output -raw db_endpoint)" \
--        -U georag -d georag -f deploy/aws/bootstrap.sql
--
-- Idempotent. Safe to re-run; re-running is the intended way to verify it.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
-- The audit behind ADR-0022's decision to use managed Postgres: of the
-- fourteen extensions the init scripts create, twelve are supported on RDS
-- for PG 18 and the two that are not (pg_ivm, pg_stat_kcache) have ZERO call
-- sites in this repository. They are absent below deliberately, not by
-- oversight — see the ADR's table.
--
-- auto_explain is also absent, and also deliberately: on RDS it is a
-- shared_preload_libraries parameter, not a CREATE EXTENSION, and it is set
-- in deploy/aws/terraform/data.tf's parameter group. It has no reader in
-- this repository either way.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;
CREATE EXTENSION IF NOT EXISTS pg_repack;

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
-- Required BY h3_postgis, which is why it is here even though no code in
-- this repository reads a raster.
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- The extension that was the whole reason managed Postgres looked risky.
-- On Azure Flexible Server `h3` sat outside the `azure.extensions`
-- allow-list, so gold.h3_density_mineral and silver.density_choropleth_h3
-- stayed raw-SQL-only and the H3 heatmap was capability-gated. RDS for
-- PG 18 supports it; if this statement fails, that assumption was wrong and
-- the H3 surfaces go back to being gated.
CREATE EXTENSION IF NOT EXISTS h3;
CREATE EXTENSION IF NOT EXISTS h3_postgis;

CREATE SCHEMA IF NOT EXISTS partman;
CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;

-- pg_cron drives pg_partman's maintenance here, not pg_partman_bgw: found on
-- a real first apply (2026-09-16) that RDS rejects pg_partman_bgw in
-- shared_preload_libraries outright (not in its allow-list at all -- RDS
-- does not run third-party background workers). pg_cron IS on that
-- allow-list, and deploy/aws/terraform/data.tf now sets
-- shared_preload_libraries=pg_stat_statements,pg_cron,auto_explain plus
-- cron.database_name=georag so pg_cron's scheduler runs against this
-- database. CREATE EXTENSION pg_cron and cron.schedule(...) both have to
-- run here, in the cron.database_name target, per RDS's documented pg_cron
-- setup -- not in the postgres maintenance DB the self-managed convention
-- uses.
--
-- Without this, partman.create_parent() (audit.audit_ledger,
-- workflow.workflow_runs, usage.usage_events -- see database/raw/phase0/
-- 20/30/60-layer-*.sql) configures partitioning but nothing ever calls
-- partman.run_maintenance_proc() to act on it: no new partitions get
-- created and none of the configured retention actually drops old ones.
-- Not a boot-time failure -- an inserts-start-failing-months-later one,
-- the day the last pre-created partition runs out.
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Idempotent: cron.schedule() upserts by job name as of pg_cron 1.4+, but
-- unschedule-then-schedule works across the version range and reads
-- unambiguously on a second run of this file.
SELECT cron.unschedule(jobid) FROM cron.job WHERE jobname = 'partman-maintenance';
SELECT cron.schedule(
    'partman-maintenance',
    '0 3 * * *',
    $$CALL partman.run_maintenance_proc()$$
);

-- ---------------------------------------------------------------------------
-- georag_app -- the role every application container connects as
-- ---------------------------------------------------------------------------
-- This used to be delegated to database/raw/phase1/10-georag-app-role.sql,
-- "applied by db:apply-raw". It is not applied by anything:
--
--   * database/raw/manifest.json lists five files, all phase0/*. The manifest
--     is deliberately an explicit list rather than a glob (its own _doc says
--     so), and phase1/10 was never added to it. ApplyRawSql walks the
--     manifest only, so that file has never run.
--   * Even if it were listed, it could not win. The migrate task is
--     `php artisan migrate --force && php artisan db:apply-raw`
--     (services.tf:616) -- migrations FIRST. Migration 2026_04_09_173750
--     runs `CREATE ROLE georag_app NOLOGIN` on any pgsql connection (its
--     only guard is a driver check; the _for_test_db in its name is
--     misleading). phase1/10's CREATE is wrapped in IF NOT EXISTS, so by the
--     time it ran the role would already exist, NOLOGIN, and it would skip.
--   * README Step 2 says `ALTER ROLE georag_app PASSWORD ...`. Verified
--     against PostgreSQL: that leaves rolcanlogin = false. A password on a
--     NOLOGIN role is not a login.
--
-- So on a fresh account every application container -- laravel-octane,
-- laravel-horizon, laravel-reverb, fastapi, hatchet-worker -- comes up and
-- dies on `FATAL: role "georag_app" is not permitted to log in`. CI never
-- sees it: ci.yml applies only phase0/*.sql and migrates as the owner.
--
-- Created here instead, before the migration chain, so 173750's IF NOT
-- EXISTS finds it present and leaves it alone. NOSUPERUSER NOBYPASSRLS is
-- not decoration: phase1/10 ends in a block that RAISES if this role ever
-- holds either, because it is the role RLS tenant isolation is enforced
-- against.
--
--   \set georag_app_password `aws secretsmanager get-secret-value \
--        --secret-id georag/app --query SecretString --output text \
--        | jq -r .GEORAG_APP_PASSWORD`

SELECT format(
         'CREATE ROLE georag_app LOGIN PASSWORD %L '
         'NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE', :'georag_app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_app')\gexec

ALTER ROLE georag_app LOGIN PASSWORD :'georag_app_password';
ALTER ROLE georag_app NOSUPERUSER NOBYPASSRLS;

-- ---------------------------------------------------------------------------
-- Grant-holder roles (docker/postgresql/init/init-roles.sql)
-- ---------------------------------------------------------------------------
-- NOLOGIN by design: they hold grants, they are not connected as. The
-- grants themselves follow the schemas, so they are applied by
-- init-roles.sql's later half and by ALTER DEFAULT PRIVILEGES — run that
-- file after the first `migrate`, once the schemas exist.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_read') THEN
    CREATE ROLE georag_read NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_write') THEN
    CREATE ROLE georag_write NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_audit') THEN
    CREATE ROLE georag_audit NOLOGIN;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- martin_readonly -- the one "grant-holder" role that IS connected as
-- ---------------------------------------------------------------------------
-- Migration 2026_04_22_130000_create_silver_mvt_functions creates this role
-- NOLOGIN, alongside georag_read/write/audit, under the same "they hold
-- grants, they are not connected as" reasoning stated above.
--
-- That reasoning is wrong for this one role. Terraform hands the martin task
-- a DATABASE_URL that connects AS martin_readonly:
--
--   config.tf:36    "MARTIN_DATABASE_URL connects as martin_readonly"
--   config.tf:163   valueFrom ...:MARTIN_DATABASE_URL:: -> DATABASE_URL
--   README.md:656   "Full connection string, as martin_readonly."
--
-- Nothing anywhere granted it LOGIN. Repo-wide, martin_readonly appears 40x
-- as a GRANT target and exactly once as a role definition -- the NOLOGIN one.
-- So the martin task got `FATAL: role "martin_readonly" is not permitted to
-- log in` and failed its health check (services.tf:371).
--
-- Compose hid it. docker-compose.yml:1993 connects martin as georag_app, so
-- dev worked and only production would have broken.
--
-- THIS IS NECESSARY AND NOT SUFFICIENT, and the second half is not fixed
-- here because it is a decision rather than a defect. The tile functions are
-- SECURITY INVOKER and set no GUC, so once this role CAN connect it runs as
-- itself: non-superuser, NOBYPASSRLS, with app.workspace_id unset. The
-- tenant policy is
--
--   USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
--
-- and against an unset GUC that predicate is NULL, so it filters every row.
-- Verified on PostgreSQL 16: same role, same table, 0 rows with the GUC
-- unset and the correct rows with it set. So the map is blank either way --
-- this changes WHICH failure, from "cannot connect" to "connects and is
-- shown nothing", and the second one is at least diagnosable.
--
-- Closing it needs a call on how workspace context reaches Martin, which
-- connects to Postgres directly and never sees the session that authorised
-- the request. docs/architecture/appendix/C-security-posture.md already
-- records that fence as Open. Do not resolve it by granting BYPASSRLS here:
-- that turns a blank map into a cross-tenant one.
--
-- Created here, BEFORE the migration chain runs, so the migration's
-- IF NOT EXISTS guard finds it present and leaves it alone. The ALTER also
-- repairs an existing database where the migration already created it
-- NOLOGIN -- which is every database that exists today.
--
--   \set martin_password `aws secretsmanager get-secret-value \
--        --secret-id georag/app --query SecretString --output text \
--        | jq -r .MARTIN_DATABASE_URL | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|'`
--
-- It must equal the password inside MARTIN_DATABASE_URL, because that string
-- is what the task actually connects with.

SELECT format(
         'CREATE ROLE martin_readonly LOGIN PASSWORD %L '
         'NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE', :'martin_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'martin_readonly')\gexec

ALTER ROLE martin_readonly LOGIN PASSWORD :'martin_password';

-- ---------------------------------------------------------------------------
-- Hatchet's own role and database
-- ---------------------------------------------------------------------------
-- Hatchet runs with SERVER_MSGQUEUE_KIND=postgres — there is no RabbitMQ in
-- this stack — so this database is both its schema store AND its message
-- queue. Without it the engine starts, fails to migrate, and the worker
-- registers nothing: 51 workflows and 29 crons quietly do not exist.
--
-- The compose default password is the literal string 'hatchet', which is
-- fine for a laptop and not for this. Set it from Secrets Manager, at the
-- top of your psql session:
--
--   \set hatchet_password `aws secretsmanager get-secret-value \
--        --secret-id georag/app --query SecretString --output text \
--        | jq -r .HATCHET_DATABASE_URL | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|'`
--
-- Out of the URL, not out of a HATCHET_DB_PASSWORD key -- there is no such
-- key in georag/app. That name is compose-only, and `jq -r` on a missing key
-- prints the string `null`, which would set this role's password to the
-- literal four characters `null` from a command that appeared to succeed.
--
-- NOT INSIDE A DO BLOCK. psql substitutes :'var' in its own lexer, and its
-- lexer treats a dollar-quoted body as one opaque token -- so a :'var'
-- written inside DO $$ ... $$ is passed through verbatim and the server
-- answers `syntax error at or near ":"`. This block used to be written that
-- way. With \set ON_ERROR_STOP on at the top of this file, that aborted
-- bootstrap.sql here: the first command of the whole go-live sequence,
-- failing before the hatchet database exists, which is what the engine
-- needs to migrate before 51 workflows can register.
--
-- So the substitution happens at the top level, where it works, and the
-- create is made conditional with \gexec instead of PL/pgSQL. format(%L)
-- quotes the value as a literal, so a password containing a quote is safe.
-- The ALTER then runs unconditionally, which is what makes re-running this
-- file repair a role that already exists with the wrong password.

SELECT format('CREATE ROLE hatchet LOGIN PASSWORD %L', :'hatchet_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hatchet')\gexec

ALTER ROLE hatchet LOGIN PASSWORD :'hatchet_password';

SELECT 'CREATE DATABASE hatchet OWNER hatchet'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'hatchet')\gexec

GRANT ALL PRIVILEGES ON DATABASE hatchet TO hatchet;

-- ---------------------------------------------------------------------------
-- Verify
-- ---------------------------------------------------------------------------
-- Prints what is present rather than asserting, so a partial run is
-- diagnosable rather than just failed.

SELECT extname, extversion FROM pg_extension ORDER BY extname;
SELECT rolname, rolcanlogin FROM pg_roles
 WHERE rolname IN ('georag', 'georag_app', 'georag_read', 'georag_write',
                   'georag_audit', 'martin_readonly', 'hatchet')
 ORDER BY rolname;
SELECT datname FROM pg_database WHERE datname IN ('georag', 'hatchet') ORDER BY datname;
