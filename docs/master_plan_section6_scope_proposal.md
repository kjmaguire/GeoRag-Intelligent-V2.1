# Master-Plan §6 (PublicGeo + MapLibre layer packs) — Scope Proposal

**Doc-phase 74** — analogous to the §5 scope proposal.

---

## What §6 ships

"The map shows public data, private data, and chat citations together — clickable, attributable."

Master plan §6 deliverables:
1. All Saskatchewan public sources verified against v2.3 schemas (mostly v1.49 baseline)
2. BC MINFILE + NRCan/GEO.ca sources added
3. Public/Private Boundary Agent enforcing §2.9 language template
4. Four layer packs: Private Project, PublicGeo, QA, Target placeholder
5. Evidence Map Mode — citations highlight map features
6. Feature Inspector with full attribute panel
7. AOI draw/buffer/cross-section line tools
8. Saved map views
9. h3-pg aggregations for density choropleths

**Done test:** chat answer cites public mineral occurrences within 25 km of project AOI using the public/private language template; map highlights the cited occurrences; clicking each shows full provenance with public/workspace tags.

---

## V1.49 baseline (checked 2026-05-13)

`public_geoscience.*` schema already populated:
- `commodity_aliases`, `jurisdictions` — reference data
- `pg_assessment_survey` + `_history` — assessment files
- `pg_bedrock_geology` + `_history` — bedrock units
- `pg_drillhole_collar` + `_history` — public drillholes
- `pg_mine` + `_history` — operating mines
- `pg_mineral_disposition` — claim dispositions
- (likely more — checked 12 of N tables)

This is significant existing infrastructure. Most of §6's
"all Saskatchewan public sources live" is ALREADY DONE.

What's actually missing per master plan:
- BC MINFILE + NRCan/GEO.ca sources (NEW data)
- Public/Private Boundary Agent (NEW agent)
- 4 layer pack definitions (NEW frontend config)
- Evidence Map Mode coupling (NEW frontend integration)
- Feature Inspector panel (NEW frontend)
- AOI draw/buffer/cross-section tools (NEW frontend)
- Saved map views (NEW backend table + frontend)
- h3-pg density aggregations (NEW Dagster asset + materialized views)

§6 is **MUCH more frontend-heavy than §5**. Backend additions are
smaller (mostly ingestion of 2 new public sources + 1 agent +
1 saved-views table); the bulk is map UI in React + MapLibre GL.

---

## Sub-step breakdown estimate

| # | What | Backend | Frontend | Ticks |
|---|---|---|---|---|
| 6.1 | Audit existing public_geoscience.* against v2.3 schemas | small | none | 1 |
| 6.2 | BC MINFILE ingestion (Kestra flow + silver/public_geoscience tables) | medium | none | 2-3 |
| 6.3 | NRCan/GEO.ca ingestion | medium | none | 2-3 |
| 6.4 | Public/Private Boundary Agent (§2.9 enforcement) | small (agent skeleton+impl) | none | 1-2 |
| 6.5 | Saved map views table + CRUD endpoints | small | none | 1 |
| 6.6 | h3-pg density aggregations Dagster asset | medium | none | 1-2 |
| 6.7 | Layer pack JSON definitions (4 packs) | small (config) | small (loader) | 1 |
| 6.8 | MapLibre map page scaffolding (Inertia React) | none | medium | 1-2 |
| 6.9 | Feature Inspector panel | none | medium | 1 |
| 6.10 | AOI draw + buffer tool (MapLibre Draw plugin) | none | medium | 1-2 |
| 6.11 | Cross-section line tool | none | medium | 1 |
| 6.12 | Evidence Map Mode — chat-citation → map-highlight wiring | small (event bus) | medium | 1-2 |
| 6.13 | Density choropleth rendering (h3-pg → tile layer) | small | medium | 1 |
| 6.14 | Saved views UI | none | small | 1 |
| 6.15 | Acceptance test against done-criterion | mixed | mixed | 1 |

**Total: 18-25 ticks.** Comparable to §5 (14-22).

Frontend skew: roughly 60% frontend, 40% backend. §5 was the opposite (~70% backend, 30% frontend).

---

## Dependencies

- **MapLibre GL JS** — listed as project dep (per CLAUDE.md, "MapLibre GL, not Mapbox GL"). Frontend npm package — check `package.json`.
- **`mapbox-gl-draw`** equivalent for MapLibre — there's a community port `@mapbox/mapbox-gl-draw` that works against MapLibre. Verify; may need a different lib.
- **h3-py** — already in Dagster pyproject (h3 extension is in the postgres image per Phase 0).
- **Martin tile server** — already running per docker-compose; need to verify v1.49 PublicGeo MVT functions still work.

---

## Risks

1. **BC MINFILE + NRCan/GEO.ca licensing** — both are Crown Copyright; the project's §22.4 SDR policy may have specific routing rules. Read §22.4 before ingesting.
2. **MapLibre GL learning curve** — if no prior MapLibre code in the project, expect a 1-2 tick spike to figure out tile layers + interaction handlers.
3. **h3-pg performance** — choropleth queries at high zoom can be slow without proper resolution-level pre-aggregation. May need to materialize multiple h3 resolutions.
4. **Evidence Map Mode wiring** — coupling chat citations to map features requires a stable event bus (Reverb private channel per project). Architectural piece worth thinking through before coding.

---

## Open questions for Kyle

1. **Order of operations**: should §6 follow §5 sequentially, or can they overlap? §6's frontend work could parallelize with §5.12 React work — same Inertia React surface.
2. **BC MINFILE + NRCan/GEO.ca priority**: master plan lists them as required for §6 done. Do you want them shipped in §6 or deferred to a §6.1 v2?
3. **Saved map views auth model**: per-user, per-project, or per-workspace? Master plan doesn't specify.
4. **Density choropleth h3 resolution**: 6 (≈37 km hex) at low zoom, 8 (≈460 m hex) at high zoom — typical default. Confirm or pick differently.

---

## Recommendation

§6 is large but well-scoped. Most backend pieces have parallels in
existing v1.49 code (`pg_*` tables exist, h3 extension is enabled,
Martin is running). The frontend is the bulk of new work.

If autonomous push continues, doc-phase 75 = §6.1 (audit existing
public_geoscience.* tables) + start §6.4 (Public/Private Boundary
Agent skeleton — small + non-frontend).

Frontend ticks should wait until Kyle is available — Inertia React
patterns + MapLibre integration have product-feel decisions worth
his review (color schemes, interaction handlers, layout density).

---

## TL;DR

§6 = mostly frontend (~60%) with backend reinforcement of existing
public-geoscience ingestion + one new agent + saved-views table.
Estimated 18-25 ticks. Frontend work should wait for Kyle; backend
ticks (6.1, 6.4, 6.5, 6.6) can proceed autonomously.
