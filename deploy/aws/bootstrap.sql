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
--   georag_app       database/raw/phase1/10-georag-app-role.sql, applied by
--                    `db:apply-raw`, which CD now runs (see
--                    deploy/aws/terraform/services.tf). Its final block
--                    RAISES if the role is ever SUPERUSER or BYPASSRLS.
--   martin_readonly  migration 2026_04_22_130000_create_silver_mvt_functions
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
-- Hatchet's own role and database
-- ---------------------------------------------------------------------------
-- Hatchet runs with SERVER_MSGQUEUE_KIND=postgres — there is no RabbitMQ in
-- this stack — so this database is both its schema store AND its message
-- queue. Without it the engine starts, fails to migrate, and the worker
-- registers nothing: 51 workflows and 29 crons quietly do not exist.
--
-- The compose default password is the literal string 'hatchet', which is
-- fine for a laptop and not for this. Set it from Secrets Manager:
--
--   \set hatchet_password `aws secretsmanager get-secret-value \
--        --secret-id georag/app --query SecretString --output text \
--        | jq -r .HATCHET_DB_PASSWORD`
--
-- and replace the literal below before running.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hatchet') THEN
    CREATE ROLE hatchet LOGIN PASSWORD :'hatchet_password';
  END IF;
END $$;

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
