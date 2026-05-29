# Chapter 09 — Martin and MapLibre

The map you see in the browser is rendered by MapLibre GL JS pulling
Mapbox Vector Tiles (MVT) from Martin, which generates them on demand by
calling Postgres functions or wrapping `ST_AsMVT()` around table queries.

## 1. Martin service

[docker-compose.yml:795](../../../docker-compose.yml).

- Image `ghcr.io/maplibre/martin:1.7.0` (digest-pinned).
- Port `${MARTIN_PORT:-3002}:3000`.
- Config: [docker/martin/martin.yaml](../../../docker/martin/martin.yaml).
- DB: direct to `postgresql:5432` (bypasses PgBouncer) as the `georag_app`
  role today; the canonical plan is to move to `martin_ro` /
  `martin_readonly` ([2026_04_22_130000_create_silver_mvt_functions.php:88](../../../database/migrations/2026_04_22_130000_create_silver_mvt_functions.php)).
- Pool size 20; tile cache 512 MiB
  ([docker/martin/martin.yaml:11-30](../../../docker/martin/martin.yaml)).
- Preferred encoding: gzip (~60-80 % smaller wire-bytes).

### Health and metrics

- Healthcheck: `wget /health` ([docker-compose.yml:824-828](../../../docker-compose.yml)).
- Metrics: `/metrics` (native since Martin 1.7) — scraped by Prometheus.
  Backs the 4 alert rules in
  [docker/prometheus/rules/martin-alerts.yml](../../../docker/prometheus/rules/martin-alerts.yml).
- Catalog: `GET /catalog` lists every source.

## 2. Source taxonomy

Martin 1.x exposes two source kinds. Both end in `ST_AsMVT(tile, '<layer>',
4096, 'geom')`.

### Function sources — `silver.pg_*_by_project` family

Used for workspace-scoped layers. Signature, per §05d:

```sql
CREATE OR REPLACE FUNCTION silver.pg_<name>_by_project(
    z integer, x integer, y integer, query_params json
) RETURNS TABLE (mvt bytea, etag_hash text)
```

The `etag_hash` half is the §05d freshness contract: client passes
`If-None-Match`, function computes a deterministic hash, returns 304 if
unchanged.

Active functions ([docker/martin/martin.yaml:57-126](../../../docker/martin/martin.yaml)):

| Function | Source | Created in |
|---|---|---|
| `silver.pg_collars_by_project` | `silver.collars` | [2026_04_22_140002:51](../../../database/migrations/2026_04_22_140002_fix_silver_mvt_function_variable_names_part2.php), uncertainty extension [2026_05_24_130000](../../../database/migrations/2026_05_24_130000_add_uncertainty_to_pg_collars_mvt.php) |
| `silver.pg_drill_traces_by_project` | `silver.drill_traces` | [2026_04_22_140002:133](../../../database/migrations/2026_04_22_140002_fix_silver_mvt_function_variable_names_part2.php) |
| `silver.pg_seismic_by_project` | `silver.seismic_surveys` | [2026_04_22_140002:223](../../../database/migrations/2026_04_22_140002_fix_silver_mvt_function_variable_names_part2.php) |
| `silver.pg_boundaries_by_project` | (stub) | [2026_04_22_140001:35](../../../database/migrations/2026_04_22_140001_fix_silver_mvt_function_variable_names.php) |
| `silver.pg_formations_by_project` | (stub) | [2026_04_22_140001:102](../../../database/migrations/2026_04_22_140001_fix_silver_mvt_function_variable_names.php) |
| `silver.pg_historic_workings_by_project` | (stub) | [2026_04_22_140001:171](../../../database/migrations/2026_04_22_140001_fix_silver_mvt_function_variable_names.php) |
| `silver.pg_geochem_by_project` | (stub) | [2026_04_22_140001:233](../../../database/migrations/2026_04_22_140001_fix_silver_mvt_function_variable_names.php) |
| `silver.pg_cross_section_lines_by_project` | `gold.cross_section_panels.section_line_geom` | [2026_05_22_020000:40](../../../database/migrations/2026_05_22_020000_add_cross_section_mvt_and_geophysics_unique.php) |
| `silver.significant_intersections_by_project` | `gold.significant_intersections` ⋈ `silver.collars` | [2026_05_20_061000](../../../database/migrations/2026_05_20_061000_create_martin_significant_intersections_function.php) |
| `silver.density_choropleth_h3` | `gold.h3_density_mineral` | [phase0/105-section6-density-mvt-function.sql:64-89](../../../database/raw/phase0/105-section6-density-mvt-function.sql) |
| `public_geo.pg_mines_tiles` … (8 wrappers) | wraps the `v_pg_*_mvt` views with the §05d signature | [2026_04_22_130000](../../../database/migrations/2026_04_22_130000_create_silver_mvt_functions.php) |

### Table sources — `public_geo.v_pg_*_mvt` views

[docker/martin/martin.yaml:222-465](../../../docker/martin/martin.yaml).
Used for the public geoscience reference layers. Martin wraps each view’s
`geom` column with `ST_AsMVT` at tile-serve time.

| Source | View | Notes |
|---|---|---|
| `pg_mines` | `public_geo.v_pg_mines_mvt` | |
| `pg_mineral_occurrences` | `public_geo.v_pg_mineral_occurrences_mvt` | |
| `pg_drillhole_collars` | `public_geo.v_pg_drillhole_collars_mvt` | `has_total_length` boolean works around the MapLibre null-int4 tile-rejection bug ([martin.yaml:316-326](../../../docker/martin/martin.yaml)) |
| `pg_rock_samples` | `public_geo.v_pg_rock_samples_mvt` | |
| `pg_assessment_surveys` | `public_geo.v_pg_assessment_surveys_mvt` | |
| `pg_resource_potential` | `public_geo.v_pg_resource_potential_mvt` | Same null-int4 trap mitigation via `has_potential_rank` |
| `pg_mineral_dispositions` | `public_geo.v_pg_mineral_dispositions_mvt` | |
| `pg_bedrock_geology` | `public_geo.v_pg_bedrock_geology_mvt` | |
| `smdi_deposits` | `public.smdi_deposits` (direct table) | Saskatchewan Mineral Deposit Index, plan v1.1 |

> `id_column` is omitted on all UUID-keyed views — MVT feature IDs are
> uint64 ([martin.yaml:255-258](../../../docker/martin/martin.yaml)). MapLibre
> `queryRenderedFeatures` still returns properties without the feature ID.

Many additional Tier 2/3 sources are pre-written but commented out
([martin.yaml:391-744](../../../docker/martin/martin.yaml)) — pending
migration + Silver materialisation; Martin 1.x crashes on unresolved table
references.

## 3. Tile freshness contract (§05d)

Every function returns `(mvt bytea, etag_hash text)`. The default
etag formula for public_geo:

```sql
SELECT EXTRACT(EPOCH FROM MAX(updated_at))::bigint::text
  FROM public_geo.jurisdictions
```

The MVT byte payload itself is deterministic — guaranteed by a pgTAP golden
snapshot test
([database/tests/pgtap/10_golden_mvt_snapshots.sql:152](../../../database/tests/pgtap/10_golden_mvt_snapshots.sql)).

## 4. The MVT-nullable-numeric convention

See [docs/mvt-nullable-numeric-convention.md](../../mvt-nullable-numeric-convention.md). Summary: MVT properties cannot carry `NULL` for
`int4`/`float8`. A NULL coerces to 0 and the entire tile fails to parse on
the MapLibre side. Convention:

- Coalesce to 0 in the view.
- Add a sibling `has_<field>: bool` property so the popup can distinguish
  "0 recorded" from "not available".

Two real bugs that triggered this convention:
- `drillhole_collars.total_length_m` (520 source rows NULL) — symptom: drillhole circles never rendered.
- `resource_potential.potential_rank` (908 rows NULL).

## 5. The Laravel tile proxy

Sanctum-protected proxy at `/tiles/public-geoscience/<source>/{z}/{x}/{y}`.
Forwards to Martin and enforces:
- Workspace tenant fence (sets `app.workspace_id` GUC).
- Authorisation per layer (some are Tier 3-gated).
- Caching headers.

Controller: [app/Http/Controllers/Tiles/](../../../app/Http/Controllers/Tiles/).
Used by every page that talks to `public_geo.*` layers.

For `silver.pg_*_by_project` (workspace-scoped) functions, the URL pattern
hits a `/tiles/silver/<source>/<workspace>/{z}/{x}/{y}` proxy that injects
`query_params` containing `workspace_id` (and `project_id`, `element`,
etc. for tools like `significant_intersections_by_project`).

## 6. MapLibre frontend integration

Library: `maplibre-gl` 5.x (peer of `react-map-gl` is **not** in play here —
this stack uses MapLibre directly). All map components live under
[resources/js/Components/Map/](../../../resources/js/Components/Map/).

### Pages that mount a map

| Page | File | Layers |
|---|---|---|
| Lakehouse map | [resources/js/Pages/Lakehouse.tsx](../../../resources/js/Pages/Lakehouse.tsx) | All silver function sources + public_geo overlays |
| DrillholeDetail | [resources/js/Pages/DrillholeDetail.tsx](../../../resources/js/Pages/DrillholeDetail.tsx) | Collars + drill_traces, plus a focused inset on the active hole |
| Foundry Workspace (3D mode) | [resources/js/Pages/Foundry/Workspace.tsx](../../../resources/js/Pages/Foundry/Workspace.tsx) | 9 sub-views (3D expansion landed 2026-05-25) |
| PublicGeo overlay | [resources/js/Pages/PublicGeoscience/PublicGeoOverlay.tsx](../../../resources/js/Pages/PublicGeoscience/PublicGeoOverlay.tsx) | All public_geo layers |
| SavedMapViews | [resources/js/Pages/SavedMapViews.tsx](../../../resources/js/Pages/SavedMapViews.tsx) | Persisted view state from `silver.saved_map_views` |
| Targets / TargetRecommendation | [resources/js/Pages/Foundry/Targets.tsx](../../../resources/js/Pages/Foundry/Targets.tsx) + [Dashboards/TargetRecommendation.tsx](../../../resources/js/Pages/Dashboards/TargetRecommendation.tsx) | Target scores ⋈ collars; significant intersections layer |

### Layer composition

For each source, MapLibre adds:
- A `vector` source: `tiles: ['<tile_url>/{z}/{x}/{y}']`, `minzoom`, `maxzoom`.
- A layer per geometry type: `circle` for points, `line` for LineString,
  `fill` + `line` for Polygon.
- Click handlers via `queryRenderedFeatures` (no feature ID needed — see
  §2 above).

Style:
- Style URL: a local MapLibre style at `public/maplibre-style.json` (or
  a self-hosted basemap). MapLibre GL (not Mapbox GL) — Hard Rule #8.
- Glyphs/sprites: bundled.

### Workspace fence

Every tile URL is workspace-scoped via the Laravel proxy. The frontend
includes a `WorkspaceContext` provider that sets the current workspace_id
on every request header; the proxy reads it and sets the
`app.workspace_id` GUC on the DB connection before calling Martin.

## 7. Saved map views

`silver.saved_map_views`
([2026_05_13_090000](../../../database/migrations/2026_05_13_090000_create_silver_saved_map_views.php)).
Stores `{center, zoom, bearing, pitch, active_layers[], filter_state}` JSONB.
UI lives at `SavedMapViews.tsx`. Has `workspace_id` + RLS.

## 8. Cross-section visualisation

The "B6" cross-section visualisation reads `gold.cross_section_panels` and
projects drillhole intervals onto the section plane. The section LINE is
served as MVT via `silver.pg_cross_section_lines_by_project` so the line
shows up on the project map too
([project_bsg_buildout_2026_05_22](../notes/INDEX.md#project_bsg_buildout_2026_05_22)).
