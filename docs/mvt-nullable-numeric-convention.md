# MVT views — nullable numeric columns

**Status:** Convention established 2026-04-16 during debugging of the Public Geoscience drillhole popup bug. Applies to every `v_pg_*_mvt` view served through Martin, and any future MVT view added to the project.

## The problem

Martin encodes declared-type MVT properties strictly. When a view column is declared (in `docker/martin/martin.yaml`) as `float8`, `int4`, `smallint`, `bool`, or `date`, and **any row** in that view has `NULL` in that column, Martin emits a MVT feature whose property value is `null`. MapLibre's tile-parsing worker then rejects the entire tile with:

```
Expected value to be of type number, but found null instead.
evaluate @ ... filter @ ... populate @ ... _parseWorkerTile
```

The failure cascades quietly: the tile worker swallows the exception, the tile never becomes feature data, no circles / polygons / lines render for that tile, `queryRenderedFeatures` returns empty, click handlers silently bail. Users see a blank map where data should be.

This class of bug bit us twice in the same afternoon:

- `pg_drillhole_collar.total_length_m` — 520 rows with NULL depth, all drillhole tiles were broken, clicks on drillholes produced no popup.
- `pg_drillhole_collar.date_drilled` — 100% (33,490) of rows had NULL. Every drillhole tile rejected; no circles rendered above zoom 5.
- `pg_resource_potential_zone.potential_rank` — 908 of 908 rows null; every resource-potential tile broken for the same reason.

## The rule

**Never expose a nullable numeric / boolean / date / timestamp column in a MVT view without guarding it.**

Two patterns, in order of preference:

### Pattern A — drop it

If the column is always-null or almost-always-null, drop it from the view. It adds bytes to every tile without carrying signal. Example: `date_drilled` for SK drillholes is 100% NULL across the dataset, so the view simply omits it:

```sql
CREATE VIEW public_geoscience.v_pg_drillhole_collars_mvt AS
SELECT
    d.id,
    -- ... non-null columns ...
    -- date_drilled intentionally excluded (100% NULL in SK feed)
    d.geom
FROM public_geoscience.pg_drillhole_collar d;
```

### Pattern B — COALESCE + paired has_* bool

If the column genuinely holds data for a subset of rows, COALESCE the NULL to a safe sentinel and emit a paired `has_*` boolean so the client can tell "zero recorded" from "not recorded":

```sql
CREATE VIEW public_geoscience.v_pg_drillhole_collars_mvt AS
SELECT
    d.id,
    -- ... other columns ...
    COALESCE(d.total_length_m, 0::numeric(10,2)) AS total_length_m,
    (d.total_length_m IS NOT NULL)               AS has_total_length,
    d.geom
FROM public_geoscience.pg_drillhole_collar d;
```

Then in the Martin yaml:

```yaml
properties:
  total_length_m: float8
  has_total_length: bool
```

And in the React popup / style layer:

```tsx
const hasDepth = properties.has_total_length === true
    || properties.has_total_length === 'true';  // handle both bool and stringified bool
return hasDepth
    ? <KV label="Depth" value={`${properties.total_length_m} m`} />
    : null;  // don't surface the 0 sentinel to users
```

## When in doubt

Run this audit query after creating or modifying any `v_pg_*_mvt` view:

```sql
-- Any row-level NULL in a non-text column exposed through the view?
SELECT table_name, column_name, data_type
  FROM information_schema.columns
 WHERE table_schema = 'public_geoscience'
   AND table_name LIKE 'v_pg_%_mvt'
   AND is_nullable = 'YES'
   AND data_type NOT IN ('character varying','text','USER-DEFINED','ARRAY','uuid');
```

Then for each offending column:

```sql
SELECT COUNT(*) FILTER (WHERE <col> IS NULL) AS nulls FROM public_geoscience.<view>;
```

If `nulls > 0`, apply Pattern A or B before the view hits production.

## Why not just let Martin skip the property?

Martin's MVT encoder handles null bools / numbers the same way MVT technically allows: it serialises them as `null` values in the feature's properties table. The MVT spec permits this. Most MVT clients handle it. **MapLibre's tile-parsing worker does not**, at least not when a style layer's filter expression evaluates `['get', 'that_column']` in a numeric comparison context. The failure is in MapLibre's evaluator, not Martin's encoder. Working around it in the view is cheaper than patching our fork of MapLibre.

## What about `last_seen_at` timestamps?

Every canonical table has a `last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` column which the MVT view projects. `information_schema` reports these as `is_nullable = YES` (Postgres can't infer `NOT NULL` through a view boundary), but the underlying table has `NOT NULL`, so the column is always populated. The audit query above would flag them as "potentially risky"; a spot-check with `COUNT(*) FILTER (WHERE last_seen_at IS NULL)` returning 0 is sufficient to clear them. No special handling required.

## Known-safe columns (audited 2026-04-16)

All 11 nullable-by-schema numeric / bool / date / timestamp columns across the six Tier 1 MVT views returned 0 NULLs in the row-count audit. Views are clean going into production.

## Tier 2+3 implication

Every new entity type scheduled for Tier 2+3 build-out (tenure, geology, petroleum, geophysics, reference data — see `HANDOFF-SK-EXPANSION.md`) that introduces a numeric or date column **must** apply this convention at MVT-view creation time. Adding it retroactively means hours of debugging a silently-broken map.
