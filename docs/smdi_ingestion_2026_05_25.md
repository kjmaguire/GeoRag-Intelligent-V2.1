# SMDI ingestion — overnight delivery, 2026-05-25

**Plan:** `~/Downloads/georag-smdi-ingestion-plan (1).md` v1.1 (2026-05-24)
**Executed by:** Claude Opus 4.7, overnight autonomous run while Kyle slept

## Duplicate cleanup — 2026-05-25 ~06:50 UTC

Kyle's "delete duplication" follow-up retired the parallel Hatchet pull workflows + the 8 synthetic-stub adapter modules that have no production callers.

**Deleted (19 files):**

Hatchet workflows:
- `src/fastapi/app/hatchet_workflows/bc_minfile_pull.py`
- `src/fastapi/app/hatchet_workflows/nrcan_geo_pull.py`

Synthetic-stub adapters:
- `src/fastapi/app/services/publicgeo/{assessment_survey_adapters,bc_drillhole_adapter,bc_minfile_adapter,bedrock_geology_adapters,nrcan_mines_adapter,sk_drillhole_adapter,sk_minoccur_adapter,usgs_mrds_adapter}.py`

Tests for the deleted code:
- `src/fastapi/tests/test_{assessment_survey_adapters,bc_drillhole_adapter,bc_minfile_adapter,bc_minfile_pull,bedrock_geology_adapters,nrcan_mines_adapter,section6_wave3_upserts,sk_drillhole_adapter,sk_minoccur_adapter}.py`

**Updated:**
- `src/fastapi/app/services/publicgeo/__init__.py` — emptied of re-exports; left as a tombstone module pointing future readers to the Dagster path
- `src/fastapi/app/hatchet_workflows/worker.py` — removed the 2 imports + pool registrations; left in-place deprecation comments

**Hatchet engine cleanup:** Disabled 4 stale cron entries on the `WorkflowTriggerCronRef` table (2 each for `bc_minfile_pull` and `nrcan_geo_pull`, from prior worker restart cycles). They no longer fire. Hatchet's engine will garbage-collect them on the next workflow-version churn — leaving the disabled rows in place is harmless.

**Verified:** FastAPI restarted clean (no import errors), `from app.services.publicgeo import *` produces no symbols (as designed), Hatchet AI worker registered with the trimmed workflow list (35 workflows, no PG pulls), `cost_burn_watcher` continues to fire on schedule.

## Untouched cleanup — 2026-05-25 ~06:40 UTC

Kyle's "fix the untouched" follow-up closed the three remaining items from the morning summary.

**(1) 7 remaining Bronze→Silver pairs smoke-tested:**

| Canonical table | source_id | Rows |
|---|---|---|
| `pg_assessment_survey` | CA-SK-ASSESSMENT-AIRBORNE | 1,799 |
| `pg_assessment_survey` | CA-SK-ASSESSMENT-GROUND | 8,490 |
| `pg_assessment_survey` | CA-SK-ASSESSMENT-UNDERGROUND | 4,546 |
| `pg_bedrock_geology` | CA-SK-GEOLOGY-BEDROCK-250K | 9,596 |
| `pg_mineral_disposition` | CA-SK-MINERAL-DISPOSITION-* (8 layers) | 30,160 |
| `pg_resource_potential_zone` | CA-SK-RESOURCE-POTENTIAL-* (10 commodities) | 908 |
| `pg_rock_sample` | CA-SK-ROCK-SAMPLES | 29,875 |

Two bugs surfaced + fixed:

- **Bedrock geology seeder URL** was missing the trailing `/10` layer index. Fixed in [CanadaJurisdictionsSeeder.php](database/seeders/PublicGeoscience/CanadaJurisdictionsSeeder.php) + patched the live row.
- **PostGIS `InsufficientResources('no empty local buffer available')`** on bulk `ST_GeomFromText` over large MULTIPOLYGON corpora (bedrock_geology = 9,596 polygons, mineral_disposition = 19,001 across 10 layers). Root cause: PG default `temp_buffers = 8 MB` overflowed on the staging TEMP table. Fix: `ALTER SYSTEM SET temp_buffers = '256MB'` + pgbouncer restart. (Per-session `SET temp_buffers` doesn't work behind pgbouncer because the underlying PG backend may have already touched temp tables in earlier transactions.)

**(2) Legacy synthetic-stub rows swept:** 104 rows deleted from canonical tables under snake_case source_ids (`sk_mineral_occurrence` ×14, `bc_minfile_mineral_occurrence` ×15, `nrcan_canadian_mines` ×12, `sk_drillhole_collar` ×12, `bc_minfile_drillhole_collar` ×10, `sk_assessment_survey` ×8, `bc_aris_assessment_survey` ×8, `nrcan_geo_bedrock_geology` ×8, `ab_ags_bedrock_geology` ×8, plus several mineral_disposition / resource_potential variants). 9 orphaned source registry rows also removed. Canonical tables now hold ONLY `CA-*` source rows totalling 120,309 features.

**(3) Public Geoscience schedules enabled** via Dagster GraphQL:
- `public_geoscience_weekly_refresh` — Sunday 03:00 UTC, full pull → **RUNNING**
- `public_geoscience_daily_edit_check` — Daily 05:30 UTC, edit-date short-circuit → **RUNNING**
- `full_ingest_schedule` (02:00 UTC, all assets) — left **STOPPED** (out of scope; big global schedule)
- `smdi_deposits_daily_refresh` (03:30 UTC, standalone path) — left **STOPPED** pending decommission decision

### Hatchet duplication flagged for separate decision

While sweeping the snake_case rows I found that some of them came from parallel Hatchet workflows still scheduled monthly:

- `bc_minfile_pull` (Hatchet, monthly cron `0 6 1 * *`) — writes to `pg_mineral_occurrence` + `pg_drillhole_collar` under snake_case source_ids `bc_minfile_*`. Now duplicates the Dagster `silver_pg_ca_bc_minfile` / `silver_pg_ca_sk_drillhole` work.
- `nrcan_geo_pull` — similar pattern for NRCan.
- Synthetic-stub adapters (`sk_minoccur_adapter.py`, `sk_drillhole_adapter.py`, `bedrock_geology_adapters.py`, `assessment_survey_adapters.py`, `bc_drillhole_adapter.py`, `nrcan_mines_adapter.py`) — defined but have no production callers I could find.

The Hatchet workflows weren't disabled (out of scope, and they have value as an alternative ingest path if Dagster has issues). Their next monthly fire on **2026-06-01 06:00 UTC** will write fresh `bc_minfile_*` rows back into the canonical tables alongside the canonical `CA-BC-MINFILE` rows. To prevent this, either:
- Comment out `on_crons` in the two Hatchet workflow files, OR
- Update their target source_id resolution to write under `CA-*` source_ids (true unification)

Flag for the morning queue.

## Morning update — 2026-05-25 ~06:20 UTC

Kyle's "fix the legacy bronze assets" follow-up landed Path B (canonical unification). The 6 Dagster PG asset files had stale `public_geoscience.*` schema references left behind by the [2026-05-17 schema rename migration](../database/migrations/2026_05_17_120100_rename_public_geoscience_to_public_geo.php); the seeders had never been run against the live DB. After a 171-occurrence word-boundary rename + running the three CA seeders, the full Bronze→Silver pipeline now writes real data into the canonical lakehouse:

| Canonical table | source_id | Rows |
|---|---|---|
| `public_geo.pg_mine` | `CA-SK-MINE-LOC` | 140 |
| `public_geo.pg_mineral_occurrence` | `CA-SK-SMDI` | 6,012 |
| `public_geo.pg_mineral_occurrence` | `CA-BC-MINFILE` | 16,261 |
| `public_geo.pg_drillhole_collar` | `CA-SK-DRILLHOLE` | 33,490 |

The standalone `public.smdi_deposits` table from last night is now redundant (the canonical `pg_mineral_occurrence` row counts agree to the feature). It's left in place for the moment — it's harmless and the Public Geoscience map exposes both layers. Decommissioning is a quick follow-up (drop the table + migration + Dagster asset + Martin source + frontend layer + FastAPI router); flag when you want it done.

The pre-existing 14 SK + 15 BC + 12 NRCan synthetic-stub rows still coexist in the canonical tables under their snake_case source_ids; they're orphaned (their seeder source rows live alongside the canonical CA-* ones) but harmless. Sweepable later.

## Original overnight delivery

## TL;DR

All six phases shipped. Live SMDI data now flowing into PostGIS, served via Martin, surfaced in the Public Geoscience UI, and queryable through a FastAPI endpoint. **6,012 real Saskatchewan mineral deposits live in `public.smdi_deposits` as of 05:36 UTC on 2026-05-25.**

**One architectural decision needs your call**, documented in §"Unification question" below.

## What got built

| Phase | Artifact | Status |
|---|---|---|
| 1 | `public.smdi_deposits` table (19 cols + 5 indexes, PostGIS GEOMETRY) | Live |
| 2 | `smdi_deposits_refresh` Dagster asset (count-gated paginated upsert) | Live, bootstrapped |
| 3 | `smdi_deposits_daily_refresh` schedule, cron `30 3 * * *` UTC | Registered, **STOPPED** (default — enable in Dagster UI after morning review) |
| 4 | Martin `smdi_deposits` tile source at `/smdi_deposits/{z}/{x}/{y}` | Live, 200 OK, 4.2 KB MVT for z=5/x=6/y=11 |
| 5 | Public Geoscience layer toggle "SMDI Mineral Deposits (SK)" | Built, vite-rebuilt, octane reloaded, **default OFF** |
| 6 | `GET /public-geo/smdi/features` FastAPI endpoint | Live, 130 ms for full 6,012 (PostGIS-side JSON assembly) |

### Files added

- `database/migrations/2026_05_25_050000_create_smdi_deposits.php`
- `src/dagster/georag_dagster/assets/smdi_deposits.py`
- `src/fastapi/app/routers/smdi.py`
- `docs/smdi_ingestion_2026_05_25.md` (this doc)

### Files modified

- `src/dagster/georag_dagster/definitions.py` — registered asset + schedule
- `docker/martin/martin.yaml` — new `smdi_deposits` table source
- `app/Http/Controllers/PublicGeoscience/TileProxyController.php` — whitelist
- `resources/js/Components/PublicGeoscience/publicGeoscienceLayers.ts` — LayerId + LAYER_SPECS + SMDI_STYLE + SMDI_GROUPING_MATCH_EXPR + `colorExpr` param to `pointLayers()`
- `resources/js/Components/PublicGeoscience/PublicGeoscienceMap.tsx` — style/colorExpr dispatch + filter exemption
- `src/fastapi/app/main.py` — router include

## Verified state

```
$ docker exec georag-postgresql psql -U georag -d georag -c \
    "SELECT COUNT(*), COUNT(*) FILTER (WHERE production = true) FROM public.smdi_deposits;"
 count | count
-------+-------
  6012 |   142

$ docker exec georag-postgresql psql -U georag -d georag -c \
    "SELECT symbology_grouping, COUNT(*) FROM public.smdi_deposits GROUP BY 1 ORDER BY 2 DESC;"
  symbology_grouping  | count
----------------------+-------
 Base Metals          |  1786
 Uranium              |  1698
 Precious Metals      |   900
 Coal                 |   482
 Industrial Materials |   402
 Other                |   249
 Rare Earth Elements  |   212
 Gemstones            |   150
 Helium               |    86
 Potash / Salt        |    28
 Lithium              |    19
```

Three Dagster runs of the asset verified the plan's acceptance gates:
1. **Bootstrap** (`force=True`, empty table) — 6,012 rows in 6.2 s
2. **Idempotency** (`force=True`, populated table) — 6,012 rows stable, no dupes, `updated_at` bumped
3. **Skip path** (`force=False`, populated table) — exits in 0.47 s with `status=skipped, reason=no_change`

FastAPI endpoint verified all three contracts:
| Query | Count | Bytes | Latency |
|---|---|---|---|
| `?producers_only=true` | 142 | 72 KB | 0.07 s |
| `?commodity_group=Uranium` | 1,698 | 840 KB | 0.26 s |
| unfiltered | 6,012 | 2.95 MB | 0.13 s |

Martin tile endpoint verified:
```
$ wget http://localhost:3000/smdi_deposits/5/6/11
HTTP/1.1 200 OK
content-length: 4282
content-type: application/x-protobuf
```

## Plan-to-codebase reconciliations

The plan was written without recon of the existing geo pipeline. Three adaptations were necessary; all are documented inline at the relevant call site:

### 1. Raw `migrations/NNNN_*.sql` → Laravel migration on `pgsql_migrations`

The plan specified raw SQL migrations. The repo uses Laravel migrations executed via the dedicated `pgsql_migrations` connection (owner role) per `config/database.php`. The migration follows the `bronze_manifest` pattern: pgsql driver guard + raw `DB::statement` DDL.

### 2. Kestra Python script → Dagster asset

The plan called for a Kestra task running a Python script. `CLAUDE.md` rule 7 ("Don't duplicate orchestration") explicitly assigns scheduled/bulk data pipelines to **Dagster**, not Kestra. Kestra's existing role in this repo is Kestra→Hatchet bridging via FastAPI JWTs (`kestra/flows/georag/public_geoscience_pull.yaml`), which doesn't fit a self-contained upsert pipeline. The Dagster asset reuses the existing `arcgis_rest` client + `PostgresResource`.

### 3. `httpx.AsyncClient` + `asyncpg` → sync `httpx.Client` + `psycopg2`

Dagster asset execution is synchronous (per the comment in `src/dagster/georag_dagster/resources.py`). The existing `arcgis_rest` client is sync. The plan's async sketch doesn't fit the Dagster execution model. The asset uses sync drivers throughout; FastAPI keeps asyncpg for the Phase 6 endpoint.

## Unification question — needs your call

This is the only architectural decision the overnight run pushed onto your morning queue.

**The repo already has a canonical mineral-occurrence table:**

- `public_geo.pg_mineral_occurrence` (snake_case schema, multi-jurisdiction, history-tracked, part of the Bronze→Silver lakehouse)
- A complete Bronze→Silver Dagster pipeline (`bronze_pg_ca_sk_smdi`, `silver_pg_ca_sk_smdi`) that *intends* to feed it from the same upstream
- A canonical layer in the existing Public Geoscience map labeled **"Mineral Occurrences"** (with description "Showings, prospects, deposits (SMDI)")
- A Martin tile source for it (`pg_mineral_occurrences`)
- The Laravel `TileProxyController`, FastAPI `sk_minoccur_adapter.py`, citation resolver `MineralOccurrenceResolver`, and several seeders/tests

**But the existing pipeline has never run with real upstream data.** As of last night:

```
$ docker exec georag-postgresql psql -U georag -d georag -c \
    "SELECT source_id, COUNT(*) FROM public_geo.pg_mineral_occurrence GROUP BY 1;"
       source_id           | count
---------------------------+-------
 bc_minfile_mineral_occurrence |    15
 sk_mineral_occurrence         |    14
```

Those 14 rows are **synthetic stubs** hard-coded in `src/fastapi/app/services/publicgeo/sk_minoccur_adapter.py`. The Bronze asset's expected `CA-SK-SMDI` source_id doesn't match the live registry's `sk_mineral_occurrence` source_id, so the real Bronze→Silver path has never executed.

**The plan's `public.smdi_deposits` is now a parallel, fully-working SK-only pipeline.** It deliberately does NOT touch `pg_mineral_occurrence`. The two coexist; the Public Geoscience UI exposes both as separate toggleable layers.

### Three paths from here — your call

**Path A — keep them parallel.** Minimum risk, two-layer UI is mildly confusing but each layer has a clear, distinct label ("Mineral Occurrences" stays the canonical lakehouse view; "SMDI Mineral Deposits (SK)" is the raw live feed). Easiest to live with for a week while you decide on Path B/C.

**Path B — consolidate into `pg_mineral_occurrence`.** Replace the parallel `smdi_deposits` table by fixing the source_id mismatch (rename or alias) and getting the existing `bronze_pg_ca_sk_smdi` Dagster asset to run with real upstream. Architecturally cleanest, but requires aligning the existing schema (`pg_mineral_occurrence` uses `primary_commodities text[]` whereas SMDI feeds it as a comma-separated string) and verifying nothing downstream (resolvers, seeders, etc.) breaks. Estimated 2-4 h of careful work.

**Path C — kill the synthetic stub, promote `smdi_deposits` to canonical.** Drop the unused public_geo SK assets entirely, treat `public.smdi_deposits` as the SK source of truth. Worst option — breaks the multi-jurisdiction lakehouse abstraction and orphans the BC MINFILE work.

**Recommendation: Path A this week, Path B next week.** The parallel architecture is functional today and the consolidation needs careful schema alignment that's not a 2am job.

## Schedule activation

The Dagster schedule `smdi_deposits_daily_refresh` is registered but **stopped** (default). To activate:

1. Dagster UI → **Schedules** → `smdi_deposits_daily_refresh` → toggle on
2. Verify next execution shows 03:30 UTC tomorrow
3. (Optional) Trigger a manual run via the asset detail page — should report `status=skipped` (count unchanged from last night's bootstrap)

The plan's open questions answered

These were resolved unilaterally per the [aggressive interpretation](C:\Users\GeoRAG\.claude\projects\C--Users-GeoRAG\memory\feedback_aggressive_interpretation.md) feedback. Reverse any you disagree with:

1. **Proxy vs. direct fetch?** FastAPI proxy at `/public-geo/smdi/features` — reads from local PostGIS (faster, upstream-friendly, already 24h-fresh via Dagster).
2. **Layer default state?** OFF by default — 6,012 points dominate visually at provincial zoom.
3. **Popup link behavior?** Carried forward from existing PG popup pattern. The WEBLINK field surfaces in the FeaturePopupCard when a SMDI point is clicked.
4. **Filter panel placement?** Inline in the existing right-rail LayerTogglePanel.

## Known limitations

1. **Frontend not browser-verified.** The vite build succeeded and the Octane workers reloaded, but no human has yet loaded the Public Geo page and clicked the SMDI toggle. The code path is exercised by `LAYER_SPECS` tests but the visual rendering of 6,012 points (which the existing tiered heatmap+circle pattern handles, NOT MapLibre clustering) needs an eyeball check. Acceptance gate from the plan §5 verify-on-implementation list is **untested**.

2. **No commodity-grouping client filter for SMDI.** The existing commodity-grouping right-rail filter is scoped to the canonical snake_case `commodity_grouping` field. SMDI uses upstream `symbology_grouping` (TitleCase). I exempted the SMDI layer from the filter — selecting "Gold" in the right rail hides Gold points on the canonical layer but doesn't filter SMDI. A follow-up should add a value-mapping table (`'precious_metals' ↔ 'Precious Metals'`) so the filter applies to both layers consistently.

3. **`bronze_pg_ca_sk_smdi` Dagster asset still broken.** Not touched. Existing source_id mismatch (`CA-SK-SMDI` in code vs. `sk_mineral_occurrence` in registry) remains. Path B above is the fix.

4. **No automated tests for the new Dagster asset.** Bootstrap + idempotency + skip-path verified manually via `dagster.materialize()`. A proper unit test fixture against an httpx mock is a follow-up.

## Verification commands

Quick end-to-end smoke if you want to retest yourself:

```bash
# Row count
docker exec georag-postgresql psql -U georag -d georag -c \
  "SELECT COUNT(*) FROM public.smdi_deposits;"
# → 6012

# Tile endpoint
docker exec georag-martin sh -c 'wget -SO /tmp/t.mvt http://localhost:3000/smdi_deposits/5/6/11 2>&1' | grep HTTP
# → HTTP/1.1 200 OK

# Manual Dagster run (skip path)
docker exec georag-dagster-daemon python -c "
from dagster import materialize, RunConfig
from georag_dagster.assets.smdi_deposits import smdi_deposits_refresh, SmdiRefreshConfig
from georag_dagster.resources import PostgresResource
import os
materialize(
    [smdi_deposits_refresh],
    resources={'postgres': PostgresResource(password=os.environ['POSTGRES_PASSWORD'])},
    run_config=RunConfig(ops={'smdi_deposits_refresh': SmdiRefreshConfig(force=False)}),
)"
# → 'SMDI refresh: upstream count unchanged (6012) — skipping full fetch'

# FastAPI endpoint
docker exec georag-fastapi python -c "
import time, jwt, urllib.request, json
from app.config import settings
token = jwt.encode(
    {'iss': 'georag-laravel', 'aud': 'georag-fastapi', 'sub': 't', 'iat': int(time.time()), 'exp': int(time.time())+60},
    settings.FASTAPI_SERVICE_KEY, algorithm='HS256', headers={'kid': settings.FASTAPI_SERVICE_KEY_KID})
req = urllib.request.Request(
    'http://localhost:8000/public-geo/smdi/features?producers_only=true',
    headers={'X-Service-Key': settings.FASTAPI_SERVICE_KEY, 'Authorization': f'Bearer {token}'})
data = json.loads(urllib.request.urlopen(req, timeout=30).read())
print('producers count =', data['feature_count'])"
# → producers count = 142
```
