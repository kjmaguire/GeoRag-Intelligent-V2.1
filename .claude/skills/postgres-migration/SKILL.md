---
name: postgres-migration
description: Laravel migration patterns plus raw SQL conventions for PostgreSQL 18 + PostGIS 3.6 features specific to GeoRAG. Use when adding or editing migrations under database/migrations/, when writing raw SQL companion files in database/raw/, when introducing partitioned tables via pg_partman, RLS policies for workspace tenancy, pg_trgm indexes, silver-schema MVT (Mapbox Vector Tile) functions for Martin, or when granting role privileges (martin_ro, etc.). Triggers on tasks involving Schema::create, Schema::table, raw SQL DDL, ALTER TABLE, CREATE FUNCTION, CREATE POLICY, partition templates, or DROP/RENAME of geological domain tables.
metadata:
  origin: GeoRAG project — derived from the 76 migrations under database/migrations/ and the silver-schema MVT function suite added 2026-04-22.
  authoritative-sources:
    - georag-architecture.html §04e (Core Data Schemas — 9 PostGIS schemas)
    - georag-architecture.html §04f (Knowledge Graph Entity Model — Neo4j label canonicalisation rules)
    - CLAUDE.md hard rules #2 (async-native drivers in FastAPI), #6 (schemas are contracts), #9 (Neo4j Community only)
    - docker/postgresql/init/Z_activate_threadripper_tuning.sql (cluster-level tuning baseline)
    - docs/RUNBOOK.md "PostgreSQL access control" section (when present)
  scope: Laravel-side migrations. PostGIS extension features (geometry, geography). Raw SQL files for PG-specific features Eloquent's Schema builder can't express cleanly.
  see-also:
    - georag-schema-contracts (canonical §04e/§04f field contracts — read first when touching domain tables)
---

# GeoRAG PostgreSQL Migration Patterns

Laravel's `Schema::create` and `DB::statement` cover the easy ~70%. The other 30% — partitions, RLS, GIST indexes on geometry, MVT functions for Martin, conditional grants — are PG-specific and live in raw SQL companion blocks.

> **Rule of thumb:** if a migration touches a §04e domain table, **start by reading `georag-schema-contracts`** for the field contract. Migrations don't decide the schema; §04e does.

## When to Apply

Activate when:
- Adding a new migration in `database/migrations/`
- Writing raw SQL under `database/raw/` (or inline in `DB::statement`)
- Introducing a partitioned table (pg_partman parent + child template)
- Adding/changing an RLS policy (`CREATE POLICY ... USING (workspace_id = current_setting('app.workspace_id')::uuid)`)
- Adding an MVT function in the `silver` schema for Martin
- Granting role privileges (`martin_ro`, `dagster_rw`, etc.)
- Renaming or dropping a §04e/§04f domain table — **don't** without checking `georag-schema-contracts` and SME approval first

## File layout

```
database/
├── migrations/
│   └── YYYY_MM_DD_HHMMSS_<verb>_<noun>.php   # standard Laravel; up() + down()
└── raw/
    └── YYYY_MM_DD_HHMMSS_<verb>_<noun>.sql   # PG-specific companion called from up()
```

The raw SQL file is invoked from the migration:
```php
public function up(): void
{
    DB::unprepared(file_get_contents(database_path('raw/2026_05_18_create_silver_some_view.sql')));
}
```

`unprepared` is required for multi-statement raw SQL.

## Standard migration shell

```php
<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('foo_bar', function (Blueprint $table): void {
            $table->ulid('id')->primary();
            $table->foreignUlid('workspace_id')->constrained('workspaces')->cascadeOnDelete();
            $table->foreignUlid('project_id')->constrained('projects')->cascadeOnDelete();
            // ... domain fields per §04e ...
            $table->timestampsTz();
        });

        // Index strategy
        DB::statement("CREATE INDEX foo_bar_workspace_idx ON foo_bar (workspace_id)");
        DB::statement("CREATE INDEX foo_bar_project_idx ON foo_bar (project_id)");

        // RLS — see "RLS template" below
        DB::statement("ALTER TABLE foo_bar ENABLE ROW LEVEL SECURITY");
        DB::statement(<<<'SQL'
            CREATE POLICY foo_bar_workspace_isolation ON foo_bar
                USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
                WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid)
        SQL);
    }

    public function down(): void
    {
        Schema::dropIfExists('foo_bar');
    }
};
```

## Verification block (mandatory at end of each migration)

Every migration writes a verification block as a comment at the bottom that captures the manual smoke commands. Future operators run these from `psql -h pgbouncer -U georag` to verify the schema landed cleanly:

```php
        // Verification (run after migrate):
        //   \d foo_bar
        //   SELECT count(*) FROM foo_bar;
        //   SELECT polname, polcmd FROM pg_policies WHERE tablename = 'foo_bar';
        //   SELECT relrowsecurity FROM pg_class WHERE relname = 'foo_bar';
```

## RLS template (workspace-scoped tables)

Every table that holds tenant data takes the canonical RLS pattern:

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;

CREATE POLICY <table>_workspace_isolation ON <table>
    USING      (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);

-- Bypass policy for admin / Dagster (set the GUC before bulk ingest):
--   SET LOCAL app.workspace_id = '<uuid>';
```

The `true` argument to `current_setting(name, missing_ok)` returns NULL instead of erroring when the GUC is unset — important so unauthenticated paths fail closed (NULL ≠ uuid → row hidden) rather than blowing up.

`georag-schema-contracts` defines which tables MUST be workspace-scoped. Don't extend RLS to tables outside §04e without an SME signoff.

## pg_partman partition template

For high-volume time-series tables (`audit_ledger`, `query_audit_log`, etc.):

```sql
-- Parent table (Laravel migration creates it, then this raw SQL converts to partitioned):
ALTER TABLE audit_ledger PARTITION BY RANGE (created_at);

-- pg_partman registration:
SELECT partman.create_parent(
    p_parent_table  => 'public.audit_ledger',
    p_control       => 'created_at',
    p_type          => 'range',
    p_interval      => '1 month',
    p_premake       => 4,                 -- pre-create 4 months ahead
    p_template_table => 'partman.template_audit_ledger'
);

-- Retention via run_maintenance() called by Ofelia / Hatchet:
UPDATE partman.part_config
SET retention = '12 months', retention_keep_table = false
WHERE parent_table = 'public.audit_ledger';
```

The template table holds the indexes + grants that propagate to each child partition. Define it BEFORE `create_parent()`.

## GIST + spatial indexes

```sql
-- Geometry column (CRS pinned per project; see §04 + §04b):
ALTER TABLE collars
    ADD COLUMN geom geometry(POINT, 4326);

-- GIST index for spatial queries:
CREATE INDEX collars_geom_gist ON collars USING GIST (geom);

-- Generated UTM column (project.crs_epsg drives the projection):
ALTER TABLE collars
    ADD COLUMN geom_utm geometry(POINT, 32613)
    GENERATED ALWAYS AS (ST_Transform(geom, 32613)) STORED;
CREATE INDEX collars_geom_utm_gist ON collars USING GIST (geom_utm);
```

NB: §04e + §04b mandate that `projects.crs_epsg` is the source of truth for project CRS. Hardcoding `32613` everywhere is a smell — use the project's value.

## pg_trgm for fuzzy search on entity names

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX <table>_<col>_trgm
    ON <table> USING GIN (<col> gin_trgm_ops);

-- Query:
--   SELECT * FROM entities WHERE name % 'lazy edward bay' ORDER BY similarity(name, 'lazy edward bay') DESC;
```

Use for entity-resolution paths (mineral occurrences, drillhole IDs, project names).

## Silver-schema MVT functions for Martin

The `silver` schema holds curated views/functions that Martin's `function_source` config queries to render vector tiles. Each tile source has a function with this signature:

```sql
CREATE OR REPLACE FUNCTION silver.pg_<thing>_by_project(
    z integer, x integer, y integer,
    query_params json
)
RETURNS bytea AS $$
DECLARE
    result bytea;
    project_id_filter uuid := (query_params->>'project_id')::uuid;
BEGIN
    SELECT INTO result ST_AsMVT(t.*, 'pg_<thing>_by_project', 4096, 'geom')
    FROM (
        SELECT
            id,
            -- ... per-feature attribute columns ...
            ST_AsMVTGeom(
                ST_Transform(geom, 3857),               -- Web Mercator for tile space
                ST_TileEnvelope(z, x, y),
                4096, 64, true
            ) AS geom
        FROM silver.<source_table>
        WHERE
            project_id = project_id_filter
            AND geom && ST_Transform(ST_TileEnvelope(z, x, y), <source_srid>)
    ) t
    WHERE t.geom IS NOT NULL;

    RETURN result;
END;
$$ LANGUAGE plpgsql STABLE PARALLEL SAFE;

GRANT EXECUTE ON FUNCTION silver.pg_<thing>_by_project(integer, integer, integer, json) TO martin_ro;
```

Match Martin's `config.yaml` `pg.functions` map verbatim — function name, schema, signature. When the function is added/renamed, the config update lives in `docker/martin/config.yaml`.

## Role hygiene

GeoRAG uses three Postgres roles:
- `georag` — application role; owns DDL via Laravel migrations.
- `martin_ro` — read-only for Martin tile server. SELECT + EXECUTE on silver functions only.
- `dagster_rw` — Dagster ingestion role; SELECT on bronze, INSERT/UPDATE on silver via stored procedures.

When you add a new silver-schema table or function: **also add the explicit GRANT** for `martin_ro` and/or `dagster_rw`. Forgetting this surfaces as Martin restart-looping with "schema not found" errors after deploys.

## Common mistakes

1. **Forgetting `current_setting('app.workspace_id', true)` second arg.** Without `true`, queries from unauthenticated paths hard-fail instead of silently filtering — broken UX in places that legitimately have no workspace context (login, public landing).
2. **Using `Schema::dropIfExists` on a partitioned table.** Drop the children first via `partman.undo_partition()`, then the parent. Plain `dropIfExists` errors out.
3. **CRS hardcoding.** Use `projects.crs_epsg`, not literal 32613, when projecting geometries inside migrations.
4. **Missing `down()` for raw SQL migrations.** `down()` should issue the inverse `DROP` statements. If a raw migration is genuinely irreversible (data backfill), document why with `// down() intentionally empty: data backfill, replay via re-ingest.`
5. **Skipping the verification block.** Every PR review checks the bottom-of-file verification block. If it's not there, reviewer asks for it.

## Pre-commit checks

Before committing a migration:

```bash
# Pint on the .php migration:
vendor/bin/pint --dirty --format agent

# Apply locally:
php artisan migrate

# Roll back to verify down() works:
php artisan migrate:rollback

# Re-apply:
php artisan migrate

# Verify with the block at the bottom of the migration file.
```

If `down()` doesn't cleanly reverse `up()`, fix it before opening the PR — the rollback path is part of the contract.
