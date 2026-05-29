# Master-plan §6 (PublicGeo + MapLibre layer packs) — Kickoff

**Doc-phase:** TBD on Kyle approval
**Status:** PROPOSAL — no code lands until Kyle signs off
**Predecessor:** `docs/master_plan_section6_scope_proposal.md` (doc-phase 74)
**Authored:** 2026-05-16, post-§11-v1

---

## TL;DR

§6 is much further along than the original scope proposal implied.
9 public-geoscience sources are registered (BC MINFILE + NRCan + SaskGeoAtlas
+ AGS); Public/Private Boundary Agent shipped (`app/agents/phase6/`);
saved-map views shipped (Phase H4); h3 + h3_postgis extensions enabled.
The remaining autonomous-safe work is **data pulls + density
aggregations**; the heavy frontend (AOI/Feature Inspector/Evidence
Map Mode) gates on Kyle's product judgment.

---

## Reality-calibrated scope

### Already shipped (verified 2026-05-16)

| Sub-step             | Item                                           | Evidence                                              |
|----------------------|------------------------------------------------|-------------------------------------------------------|
| 6.1 (mostly)         | Saskatchewan baseline                          | 9 rows in `public_geoscience.sources` across AB/BC/FEDERAL/SK |
| 6.1 (BC MINFILE)     | BC MINFILE registered                          | `bc_minfile_drillhole_collar` + `bc_minfile_mineral_occurrence` rows |
| 6.1 (NRCan/GEO.ca)   | NRCan/GEO.ca registered                        | `nrcan_canadian_mines` + `nrcan_geo_bedrock_geology` rows |
| 6.4                  | Public/Private Boundary Agent                  | `src/fastapi/app/agents/phase6/public_private_boundary.py` |
| 6.5                  | Saved map views table + CRUD                   | `silver.saved_map_views` (Phase H4 Restore button)    |
| —                    | h3 + h3_postgis extensions                     | `pg_extension` shows both enabled                     |
| —                    | Dagster bronze/silver/gold/index assets        | `src/dagster/georag_dagster/assets/*_public_geoscience.py` |
| (Phase H4)           | MapView component + MVT layers + collar click  | `resources/js/Components/MapView.tsx`                 |
| (Phase H4)           | TrgZoneMap MapLibre choropleth                 | `resources/js/Components/Admin/TrgZoneMap.tsx`        |

### Open — autonomous-safe (this kickoff covers these)

| Sub-step | Item                                                     | Estimated ticks |
|----------|----------------------------------------------------------|-----------------|
| 6.1b     | Audit existing pg_* tables against v2.3 schemas          | 1 (doc-only)    |
| 6.2      | Full BC MINFILE pull — populate the registered tables    | 2-3             |
| 6.3      | Full NRCan/GEO.ca pull — populate registered tables      | 2-3             |
| 6.6      | h3-pg density aggregations Dagster asset                 | 2               |
| 6.13     | Density choropleth Martin function + tile layer wiring   | 2 (backend + small frontend) |
| —        | Acceptance harness mirroring section11_acceptance.sh     | 1               |
| **Total**|                                                          | **10-12 ticks** |

### Open — Kyle-gated (frontend-heavy, deferred to §6-v2)

| Sub-step | Item                                          | Why deferred                          |
|----------|-----------------------------------------------|---------------------------------------|
| 6.7      | Layer pack JSON definitions (4 packs)         | needs operator/SME pack composition   |
| 6.8      | MapLibre map page scaffolding (full one)      | Phase H4 has MapView; full page = product decisions |
| 6.9      | Feature Inspector panel                       | product-design judgment               |
| 6.10     | AOI draw / buffer tool (MapLibre Draw plugin) | adds a new client dep; lib pick is a Kyle call |
| 6.11     | Cross-section line tool                       | UX-heavy, geological-domain decisions |
| 6.12     | Evidence Map Mode coupling                    | Reverb event-bus design call          |
| 6.14     | Saved views UI on the explorer                | Phase H4 Restore is the admin half; explorer UI = product |
| 6.15     | Full done-criterion acceptance                | depends on all above                  |

---

## Sub-step detail (the 10-12 tick batch)

### §6.1b — Schema audit

**What ships:**
- A read-only audit script that walks every `public_geoscience.pg_*` table and reports column drift vs the canonical v2.3 schema in `georag-architecture.html § 04e`.
- Audit output as a markdown doc with a per-table PASS/DRIFT row.

**Acceptance:**
- Doc lands in `docs/audits/public_geoscience_schema_audit_2026_05_16.md`
- Any DRIFT row carries a one-line "to fix: ALTER TABLE …" hint.

### §6.2 — Full BC MINFILE pull

**What ships:**
- Kestra flow (or extension of an existing Dagster asset) that pulls
  the full BC MINFILE drillhole + mineral-occurrence datasets via the
  BC government's ArcGIS REST FeatureServer.
- Output: rows land in `public_geoscience.pg_drillhole_collar` and
  `pg_mineral_occurrence` with `source_id = 'bc_minfile_*'`.
- `last_refreshed_at` updated on `public_geoscience.sources`.

**Acceptance:**
- Row counts after first pull: drillhole >= 50 000, mineral_occurrence >= 10 000
  (BC MINFILE is the largest single source in the registry).
- Spot-check 3 rows for geometry validity (`ST_IsValid` = true).
- A `public_geoscience.pull.bc_minfile.completed` audit row exists.

### §6.3 — Full NRCan/GEO.ca pull

**What ships:**
- Same pattern as §6.2 against NRCan's GEO.ca canonical-mines + bedrock
  geology endpoints.
- Output rows in `pg_mine` (canonical mines) and `pg_bedrock_geology`.

**Acceptance:**
- pg_mine >= 200, pg_bedrock_geology >= 100 polygons after first pull.
- License attribution recorded in `payload.license_url` (Open Government
  Licence — Canada).

### §6.6 — h3-pg density aggregations

**What ships:**
- New Dagster gold asset `gold_h3_density_choropleth` that:
  - Aggregates `pg_mineral_occurrence` + `pg_drillhole_collar` by h3 cell
    at resolutions {5, 7, 9} (continental / regional / project scale).
  - Materialises one row per (commodity, h3_index, resolution) with
    occurrence count + drillhole count + workspace_id NULL (cross-tenant).
- Schedule: refresh nightly at 05:00 UTC (after the §11 backup window
  + cold-tier archive).

**Acceptance:**
- New table `gold.h3_density_mineral` populated with >0 rows after
  first materialisation.
- Asset has a Dagster check that asserts non-zero row count when
  the input pg_mineral_occurrence has > 100 rows.

### §6.13 — Density choropleth tile layer

**What ships:**
- Martin function `silver.density_choropleth_h3(z, x, y, resolution)`
  that filters `gold.h3_density_mineral` by zoom-appropriate resolution
  and returns MVT.
- Optional MVT layer entry in `resources/js/lib/mvtLayers.ts` so the
  MapView toggle includes "Density (heatmap)" — defaults to off.

**Acceptance:**
- Martin returns 200 with non-empty MVT for a bounding box that contains
  any pg_mineral_occurrence rows.
- MapView toggle renders the layer when enabled.

### §6 acceptance harness

**What ships:**
- `scripts/section6_acceptance.sh` mirroring `phase_h4_acceptance.sh`
  + `section11_acceptance.sh` patterns.
- 8-12 checks covering: sources registered, row counts > threshold per
  source, h3-density gold table populated, Public/Private Boundary Agent
  imports clean, density choropleth Martin function responds.

**Acceptance:**
- Exit 0 = §6-v1 surface green.

---

## Cadence proposal

| Wave | Focus                                                | Time est |
|------|------------------------------------------------------|----------|
| 1    | §6.1b schema audit doc                               | 30 min   |
| 2    | §6.2 BC MINFILE pull + audit-row + counts            | 60-90 min|
| 3    | §6.3 NRCan/GEO.ca pull + audit-row + counts          | 60-90 min|
| 4    | §6.6 h3-pg density Dagster asset + check             | 90 min   |
| 5    | §6.13 Martin function + MVT layer wiring             | 60 min   |
| 6    | §6 acceptance harness + handoff                      | 30-45 min|

Total estimate: **5-7 hours of focused work**. Fits one overnight
batch comfortably (the §11-v1 batch was ~3 hours including handoff).

---

## Hard constraints (won't violate)

- **No new client deps** — MapLibre Draw / mapbox-gl-draw is a §6-v2 item.
  Don't add npm packages tonight.
- **No SME-level decisions** — geological-domain choices (h3 resolution
  per commodity, choropleth color ramps) stay at the SME-default proposed
  here unless Kyle overrides.
- **Existing public_private_boundary agent unchanged** — it's the §2.9
  enforcer and shipped already; new code must not regress it.
- **No schema breakage to existing pg_* tables** — additive only (new
  `gold.h3_density_mineral`, not ALTER on existing tables).

---

## Locked decisions (Kyle, 2026-05-16)

All 4 open questions answered. 3 defaults; 1 explicit override.

| Decision                           | Value                                                                |
|------------------------------------|----------------------------------------------------------------------|
| h3 resolution per commodity        | **{5, 7, 9} default + {5, 7, 9, 10} for critical minerals** — Kyle deferred to geology call 2026-05-16 |
| BC MINFILE refresh cadence         | **Monthly** — cron `0 6 1 * *` UTC (first of every month at 06:00 UTC) [override from proposed quarterly] |
| License attribution                | **Yes** — every `public_geoscience.pull.*.completed` audit row carries `license_url` + `license_summary` in payload |
| `gold.h3_density_mineral` RLS      | **Cross-tenant exempt** — add to `_WORKSPACE_ID_EXEMPT` + `_RLS_EXEMPT` in `test_tenant_isolation_auditor.py` at the same commit |

These are the working defaults for all §6-v1 sub-steps. Any deviation
requires a kickoff amendment + Kyle re-sign-off.

### Critical-mineral list (case-insensitive, matched against
`pg_mineral_occurrence.primary_commodities`)

Aligned to Canada's Critical Minerals Strategy (2022) + the
exploration patterns most likely in the registered Canadian
public-geoscience sources (BC MINFILE / SaskGeoAtlas / AGS / NRCan):

| Code  | Commodity              | Why higher resolution matters                                         |
|-------|------------------------|-----------------------------------------------------------------------|
| `u`   | Uranium                | Athabasca unconformity deposits cluster tightly; res 9 aliases SMDI groups |
| `li`  | Lithium                | Pegmatite + brine + clay-host clusters at sub-5 km scale              |
| `cu`  | Copper                 | Porphyry centres + sediment-hosted: drill-grid spacing                |
| `co`  | Cobalt                 | Tied to Ni/Cu sulfide systems; cluster density mirrors them           |
| `ni`  | Nickel                 | Magmatic sulfide camps (Sudbury/Voisey's Bay) — tight drilling        |
| `ree` | Rare earth elements    | Carbonatite + alkaline complexes — extreme spatial concentration      |
| `pge` | Platinum group         | Layered intrusions — cm-to-m scale; res 10 still aggregates safely    |

Everything else (Au, Ag, Pb, Zn, Mo, K, Fe, etc.) uses the
`{5, 7, 9}` default. The `drillhole` sentinel commodity ALSO
uses `{5, 7, 9}` — drillhole density is a coarser-scale signal
than mineralisation clusters and the count vs. occurrence
distinction is meaningful at regional zoom.

### Cron schedule reconciliation

After tonight's locked defaults the Hatchet AI pool now has the
following crons (UTC):

| Cron                     | Workflow              |
|--------------------------|-----------------------|
| `*/5 * * * *`            | cost_burn_watcher     |
| `0 2 * * *`              | backup_postgres       |
| `15 2 * * *`             | backup_neo4j          |
| `30 2 * * *`             | backup_qdrant         |
| `45 2 * * *`             | backup_redis          |
| `0 3 * * *`              | backup_seaweedfs      |
| `0 4 * * *`              | cold_tier_archive     |
| `0 5 * * *`              | gold_h3_density_choropleth (planned, §6.6) |
| `0 6 1 * *`              | bc_minfile_pull (planned, §6.2)            |
| (TBD)                    | nrcan_geo_pull (planned, §6.3)             |

---

## Sign-off

If Kyle approves this kickoff as-written:

- [ ] §6-v1 = the 10-12 tick autonomous batch above
- [ ] §6-v2 = full MapLibre frontend (Feature Inspector + AOI + Evidence
      Map Mode + Saved Views UI), deferred to a Kyle-paired session
- [ ] First wave fires next autonomous run
- [ ] Acceptance script `scripts/section6_acceptance.sh` is the done-test

If kicked back: I read the open question that blocks, redraft, re-pitch.
