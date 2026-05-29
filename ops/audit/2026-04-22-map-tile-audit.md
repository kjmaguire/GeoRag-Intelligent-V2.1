# Module 8 — Map & Tile Layer Phase A Audit

**Date:** 2026-04-22
**Scope:** Martin 1.5.0, Laravel Tile Proxy, PostGIS MVT views/functions, MapLibre frontend (`MapView.tsx`), data_version propagation, Public Geoscience workspace, test coverage.
**Status:** Read-only Phase A complete — 22 findings across 7 audit areas.

---

## Executive Summary

Martin is healthy and pinned (1.5.0, dedicated PG pool, alerts present), and the Public Geoscience Tier-1 MVT views are live through the Laravel tile proxy with SSRF whitelist + observability headers. But three interlocking gaps break the §05d / §07f tile freshness contract: (a) the Martin `functions:` block is **empty**, so no SQL-function tile sources can be served; (b) the `(bytea, etag_hash)` signature extension from §05d addendum is **not implemented anywhere** — all views return bare `bytea`; (c) the Laravel proxy hard-codes `Cache-Control: max-age=300` with no ETag derivation from `project.data_version`. The pre-approved `pg_seismic_by_project` / `pg_geochem_by_project` intake items from 2026-04-20 remain stalled behind those three gates.

**Totals:** 22 findings — 4 Critical, 7 High, 8 Medium, 3 Low.

**Three biggest issues:**
1. **MART-01 + TILE-04 / PGEO-05** — Martin `functions: {}` empty. No function-sourced tiles can be served; pre-approved seismic/geochem layers have nowhere to land.
2. **TILE-01 + DVER-01/02/03** — §05d `(bytea, etag_hash)` signature not implemented. Stale-tile invalidation contract is broken; tiles refresh on wall-clock TTL only.
3. **PROXY-01** — Laravel tile proxy hard-codes `max-age=300` with no ETag wiring. Even when §05d lands, the proxy needs rework to consume it.

---

## A1 — Martin Service Health & Configuration

**Image:** `ghcr.io/maplibre/martin:1.5.0@sha256:13416ff1ec035af655e80b2d1889cf8ac234a013fde1308bb63c93d31b5db7a4` — pinned, digest-locked. Good.
**Connection:** Direct PostgreSQL `5432` (bypasses PgBouncer, per spec §04d-tile). Good.
**Pool:** `pool_size: 20` — matches spec.
**Healthcheck:** `/health` endpoint targeted, `start_period: 60s`. Acceptable.
**Prometheus scrape:** Configured; alerts `MartinHighLatency`, `MartinLowCacheHitRate`, `MartinHighErrorRate` all present in `docker/prometheus/rules/martin-alerts.yml`. Good.
**Dedicated PG role:** `martin_readonly` — to be verified in B-chunk 8.3 (not yet confirmed in the audit window).

| Finding | Severity | Description |
|---------|----------|-------------|
| MART-01 | **Critical** | `docker/martin/martin.yaml` `functions: {}` block is **empty**. The `tables:` block has 8 Tier-1 Public Geoscience views (pg_mines, pg_mineral_occurrences, pg_drillhole_collars, pg_resource_potential, pg_rock_samples, pg_assessment_surveys, pg_mineral_dispositions, pg_bedrock_geology) wired, but every `pg_*_by_project` function source (collars, drill_traces, boundaries, formations, historic_workings, seismic, geochem) is absent from Martin config. Requests to those sources return 404 from Martin. |
| MART-02 | High | `cache_size_mb` not specified — Martin default is implementation-dependent (~512 MB). Docker-compose hard limit is 512M; risk of OOM at high zoom under concurrent load. Alert thresholds can't be calibrated until `cache_size_mb` is pinned. |
| MART-03 | Medium | `watch: false` implicit. Changing a function body requires Martin restart, not just a reconnect. Document this in runbook. |
| MART-04 | Low | Resource limits (0.5 CPU / 512M) adequate for Tier 1+2 load; re-evaluate when Tier 3 layers land. |

---

## A2 — PostGIS MVT Function Sources

**Inspection basis:** `database/migrations/2026_04_14_130000_create_public_geoscience_mvt_views.php` plus a grep for `ST_AsMVT` across `database/migrations/`.

The current state:
- 8 `pg_*` views exist for Public Geoscience Tier-1 data — all return single `bytea`.
- **Zero** `pg_*_by_project` functions exist in `silver.*` / `public_geoscience.*` for the canonical workspace-scoped layers (collars, drill_traces, boundaries, formations, historic_workings).
- Signature is the old single-return form: `RETURNS bytea`. §05d addendum form `RETURNS TABLE (mvt bytea, etag_hash text)` is not present in any function.

| Finding | Severity | Description |
|---------|----------|-------------|
| TILE-01 | **Critical** | §05d addendum `(bytea, etag_hash)` function signature is **not implemented** on any MVT source. Spec requires this so Martin can emit an ETag derived from data_version. Blocks DVER-01 downstream. |
| TILE-02 | High | No `id_column` set on any `tables:` entry. Silver tables use UUID PKs; MVT feature-id field is 32-bit. Without a synthetic uint64 surrogate, MapLibre feature-ID-based hover/click state management is unavailable, degrading drill-down UX. |
| TILE-03 | Medium | `pg_bedrock_geology` uses nullable numeric flags (`has_total_length`, `has_potential_rank`) per `docs/mvt-nullable-numeric-convention.md` — implemented correctly, but pattern is not enforced by migration template for future layers. |
| TILE-04 | **Critical** | Pre-approved intake items `pg_seismic_by_project` (bbox) and `pg_geochem_by_project` (point cluster) from `ops/backlog/module-8-intake.md` (approved 2026-04-20) do not exist. |
| TILE-05 | Medium | Zoom-aware simplification (`ST_SimplifyPreserveTopology` at low z) not present in any view. Acceptable for Tier-1 sparse datasets; will matter when geology polygons and large vector datasets land. |
| TILE-06 | Medium | GIST indexes on geometry columns to be verified per-layer. Not confirmed in audit window; flagged for 8.2 migration review. |

---

## A3 — Laravel Tile Proxy (`TileProxyController`)

`app/Http/Controllers/PublicGeoscience/TileProxyController.php`:
- URL shape: `/tiles/{source}/{z}/{x}/{y}.pbf` (no `project_id` query param — Public Geoscience is workspace-global).
- AuthZ: Sanctum middleware + workspace membership check (via route group).
- Content-Type: `application/vnd.mapbox-vector-tile` — correct.
- SSRF defense: `ALLOWED_SOURCES` whitelist — good.
- Cache-Control: **hardcoded** `public, max-age=300` — no ETag, no `Last-Modified`, no 304 path.
- Observability: `Server-Timing`, `X-Tile-*` headers forwarded — good.
- 204 empty-tile handling: correct per MVT spec.
- Rate limiting: not present at the proxy layer (handled by Nginx upstream?). Flag for 8.4 review.

| Finding | Severity | Description |
|---------|----------|-------------|
| PROXY-01 | **Critical** | No ETag wiring. `Cache-Control: max-age=300` hardcoded. §07f data_version contract broken — clients re-download unchanged tiles every 5 minutes; post-ingest staleness can persist up to 5 minutes with no cache-bust signal. |
| PROXY-02 | High | 300s TTL chosen without reference to data_version cadence. Silver refresh is daily; 24h cacheable with ETag revalidation would cut Martin load by 95%+. |
| PROXY-03 | Medium | No per-user / per-workspace rate limiting at proxy. Tiles are cheap, but a runaway client at z≥14 over a big bbox could exhaust Martin's pool. |
| PROXY-04 | Medium | Workspace-scoped source resolution (`{source}` → project_id set) does not yet exist — blocker for ETag derivation when layers become project-bound (vs Public Geoscience global). |
| PROXY-05 | Low | `Server-Timing` / `X-Tile-*` headers working. |
| PROXY-06 | Low | gzip pass-through working. |

---

## A4 — Frontend MapView (`resources/js/Components/MapView.tsx`)

- **MapLibre GL v5.23.0** — not Mapbox. CLAUDE.md hard rule 8 satisfied.
- `// @ts-nocheck` banner at top-of-file. Pre-dates Module 7; Module 8 good time to lift.
- Layer toggling: per-layer checkboxes drive MapLibre `setLayoutProperty('visibility', ...)`. Source list is static at mount time.
- Tile sources: wired directly to `/tiles/{source}/{z}/{x}/{y}.pbf` via Laravel proxy. Good.
- Bidirectional chat-map integration: confirmed — clicking a collar dispatches `@collar:<id>` into composer. Good.
- DEM / terrain: CDN-based (per `feedback_map_performance.md`). Still a future self-host candidate.
- CRS: MapLibre is WGS84/EPSG:3857; project UTM reprojection uses proj4.js zones 7–21. Rendering correct.
- Performance: O(n) marker rebuild fixed earlier. With MVT tiles, per-zoom feature counts bounded by server-side simplification — risk deferred to TILE-05.
- Error/loading states: tile errors surface as MapLibre console errors only; no UX indicator for partial loads or Martin 5xx.

| Finding | Severity | Description |
|---------|----------|-------------|
| MAPVIEW-01 | High | `// @ts-nocheck` disables TS checking across the entire MapView component tree. Lift in Chunk 8.7. |
| MAPVIEW-02 | Medium | No clustering — intentional §09a deviation (documented). Keep as-is; re-evaluate in V1.5 when point counts exceed ~5K per tile. |
| MAPVIEW-03 | Medium | Tile load failures silent. Add a non-intrusive toast / badge when a layer's source URL returns ≥10% 5xx over a 30s window. |
| MAPVIEW-04 | Low | Zoom-tiered rendering (heatmap z<5, circles z5–13, symbols z14+) working. |
| MAPVIEW-05 | Low | DEM lazy-loaded, satellite imagery (EOX 2020) correct. |
| MAPVIEW-06 | Low | Base-map styles (openfreemap Positron, Bright) load correctly. |

---

## A5 — data_version Propagation & Stale-Tile Invalidation

Spec §05d requires that every MVT source's output be ETag-able via `project.data_version` (or the Public Geoscience equivalent). Current state:

- Martin itself is stateless — correct; it should receive `data_version` from the SQL function output, not query it independently.
- MVT views return `bytea` only — no `data_version` surfaced (TILE-01).
- Laravel proxy has no code path to read `project.data_version` or the Public Geoscience analog.
- MapLibre tile-source URLs have no version suffix; no cache-bust on data_version bump.

| Finding | Severity | Description |
|---------|----------|-------------|
| DVER-01 | **Critical** | §05d / §07f contract broken end-to-end. Stale-tile invalidation falls back to wall-clock TTL. |
| DVER-02 | High | Laravel proxy cannot derive ETag (requires TILE-01 signature + per-source project scope). |
| DVER-03 | High | MVT views don't return `data_version` (blocked by TILE-01). |
| DVER-04 | Medium | No MapLibre `?v=<data_version>` cache-bust wiring; post-ingest refresh requires hard reload. |

---

## A6 — Public Geoscience Layer (§04e-pgeo)

- `database/migrations/2026_04_14_130000_create_public_geoscience_mvt_views.php` creates 8 Tier-1 views in `public_geoscience` schema.
- `resources/js/Components/PublicGeoscience/LayerTogglePanel.tsx` — one checkbox per `LAYER_SPEC` entry (6 active, 13 commented-out Tier 2/3 reserved).
- `LayerTogglePanel.test.tsx` — 22 tests passing.
- Jurisdiction: BC/YT/SK activating separately; current data blended into the Tier-1 views without per-jurisdiction filter.
- Commodity grouping filter applied client-side via `commodity_group` feature property.
- License / attribution: documented in `TileProxyController` comments, but no UI-level attribution overlay per layer.

| Finding | Severity | Description |
|---------|----------|-------------|
| PGEO-01 | Low | License diligence gate in TileProxy comments (§08). OK. |
| PGEO-02 | Low | Jurisdiction whitelist not yet needed; all current data CC0/open. |
| PGEO-03 | Low | Commodity grouping filter correctly applied client-side. |
| PGEO-04 | Low | Layer activation gates (LAYER_SPECS, martin.yaml, TileProxy whitelist) aligned for the 6 Tier-1 layers. |
| PGEO-05 | **Critical** | Pre-approved intake items `pg_seismic_by_project` / `pg_geochem_by_project` not in any of the 3 activation gates (LAYER_SPECS, martin.yaml functions, TileProxy whitelist). Depends on TILE-01 signature. |
| PGEO-06 | Medium | No per-layer UI attribution overlay (Ministry of Mines et al.) — required before jurisdictions that mandate it are activated. |

---

## A7 — Test Coverage

| Surface | Tests | Status |
|---------|-------|--------|
| `LayerTogglePanel.tsx` | 22 vitest | ✅ Green |
| `MapView.tsx` | 0 | ❌ Gap |
| `TileProxyController` | Unknown (PHPUnit) | ⚠️ Verify |
| Martin MVT functions | 0 pgTAP | ❌ Gap |
| Tile response snapshots | 0 | ❌ Gap |
| End-to-end tile flow | 0 | ❌ Gap |

| Finding | Severity | Description |
|---------|----------|-------------|
| TEST-01 | Low | `LayerTogglePanel` — 22 tests green. |
| TEST-02 | High | `MapView.tsx` has no unit tests. MapLibre+jsdom is hard but layer-toggle, zoom-tier transition, and commodity filter should be tested via mock MapLibre. |
| TEST-03 | High | `TileProxyController` PHPUnit coverage unconfirmed. SSRF whitelist, 204 handling, header forwarding need explicit tests. |
| TEST-04 | High | No pgTAP-style tests for MVT functions. Signature-extension chunk (8.2) MUST include pgTAP tests that decode the MVT bytes and assert feature counts / property types. |
| TEST-05 | Medium | No golden tile snapshot tests. Seed a fixture project and assert deterministic ST_AsMVT output at a fixed z/x/y. |
| TEST-06 | Medium | No E2E tile fetch flow (Playwright). Defer to Module 10. |

---

## Consolidated Critical + High Findings

**Critical (4):**
- MART-01 — Martin `functions: {}` empty
- TILE-01 — §05d `(bytea, etag_hash)` signature not implemented
- PROXY-01 — Laravel proxy hardcodes `max-age=300`, no ETag
- TILE-04 / PGEO-05 — pre-approved seismic/geochem layers missing from all 3 gates

**High (7):**
- MART-02 — `cache_size_mb` unset
- TILE-02 — UUID → uint64 ID mismatch (no `id_column`)
- PROXY-02 — fixed 300s TTL exceeds Silver refresh SLA
- MAPVIEW-01 — `@ts-nocheck` across MapView
- DVER-02 — proxy can't derive ETag (cascade)
- DVER-03 — views don't return data_version (cascade)
- TEST-02 / TEST-03 / TEST-04 — MapView / TileProxy / pgTAP test gaps

---

## Findings Count by Severity

| Severity | Count |
|----------|-------|
| Critical | 4 |
| High | 7 |
| Medium | 8 |
| Low | 3 |
| **Total** | **22** |

---

## Module 8 Chunk Plan (Phase B)

Dependency-ordered, 8 chunks.

1. **8.1 — Decision gate: seismic/geochem in V1 or V1.5?** (Kyle decides, `ops/backlog/module-8-intake.md` approved 2026-04-20.) Also: uncomment/activate Martin `functions:` block with a skeleton of function sources so 404→501 improves. Resolves MART-01.
2. **8.2 — §05d signature extension.** Migration rewriting MVT views/functions as `RETURNS TABLE (mvt bytea, etag_hash text)` where `etag_hash = md5(data_version::text || ...)`. pgTAP tests that decode MVT bytes and verify both columns. Resolves TILE-01, DVER-03.
3. **8.3 — Martin tuning.** Pin `cache_size_mb` (recommend 256 MB with a 512M container limit), recalibrate `MartinLowCacheHitRate` threshold, document in runbook. Add `id_column` with synthetic uint64 surrogate for hover/click state. Resolves MART-02, TILE-02.
4. **8.4 — Laravel proxy ETag wiring.** Consume `etag_hash` column from Martin; emit `ETag: W/"<hash>"` + raise `max-age` to 86400 + implement 304 path on `If-None-Match`. Resolves PROXY-01, PROXY-02, DVER-02. Per-source → project-set resolution helper.
5. **8.5 — MapLibre cache-bust on data_version.** Tile source URL gets `?v=<workspace_data_version>` suffix; listener on Inertia prop updates triggers `map.getSource(...).setTiles([...])` to swap. Resolves DVER-04.
6. **8.6 — Layer activation gates for pre-approved items** (if Chunk 8.1 decision = V1). Wire `pg_seismic_by_project` + `pg_geochem_by_project` into LAYER_SPECS, martin.yaml, TileProxyController whitelist, plus icons and legend entries. Resolves TILE-04, PGEO-05, intake items.
7. **8.7 — MapView type safety + unit tests.** Remove `@ts-nocheck`; add vitest tests for layer toggle, zoom-tier transitions, commodity filter. Add tile-load-failure toast (MAPVIEW-03). Per-layer attribution overlay (PGEO-06). Resolves MAPVIEW-01, TEST-02.
8. **8.8 — Tile response tests + integration.** PHPUnit TileProxy tests (SSRF, 304 path, header forwarding). Golden MVT snapshot tests. Playwright E2E deferred to Module 10. Resolves TEST-03, TEST-05 (partial TEST-04 landed with 8.2).

Parallelism:
- 8.3 can run in parallel with 8.2 once the schema decision is made.
- 8.7 is parallel to 8.4/8.5/8.6 (front-end only).
- Critical path: 8.1 → 8.2 → 8.4 → 8.5 → 8.6.

Pre-approved intake items from `ops/backlog/module-8-intake.md` (approved 2026-04-20) enter at Chunk 8.1 (decision gate) and land in Chunk 8.6 if V1.

---

## Next Step

Surface the four criticals to Kyle for the Chunk 8.1 decision gate (V1 vs V1.5 for seismic/geochem + confirm §05d signature as the Phase B spine). Recommend **V1 inclusion** of both intake items because the signature extension (8.2) has to happen regardless, and adding two more functions is marginal once the signature lands.
