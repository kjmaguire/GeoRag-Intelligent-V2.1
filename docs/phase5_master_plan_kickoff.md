# GeoRAG Master-Plan §5 Implementation Kickoff (Spatial pipeline + drillhole visuals)

**Document version:** 1.0
**Status:** Opened doc-phase 70 (autonomous run). Per scope proposal in
`docs/master_plan_section5_scope_proposal.md`.
**Audience:** post-reset session continuing the autonomous push.

Best-judgment defaults applied (Kyle's open questions from the scope
proposal):
- Plotly: HTML embed for v1
- mplstereonet: ADD to FastAPI pyproject.toml
- §5.10 + §5.11 agents: ship as part of §5 done-test
- §5 opened in parallel with Step 9 (Kyle's labeling work)

---

## §5.1 dependency audit results (doc-phase 70)

Checked the running `georag-fastapi` container 2026-05-13 ~06:35 UTC:

| Library | In FastAPI? | In Dagster pyproject | Status |
|---|---|---|---|
| `geopandas` | ❌ ModuleNotFoundError | ✅ `>=1.0` | **MUST ADD to FastAPI** |
| `rasterio` | ❌ ModuleNotFoundError | ✅ `>=1.4` | **MUST ADD to FastAPI** |
| `shapely` | ✅ 2.1.2 | ✅ | already present |
| `mplstereonet` | ❌ | ❌ | **NEW dep — MIT-licensed, add to FastAPI** |
| `plotly` | not checked | not checked | TBD by parser-path; HTML embed = no Python dep |

Master plan §5 deliverable #1 ("GeoPandas/Rasterio/Shapely integrated
into FastAPI ingestion paths") requires the missing two. Worth a
container rebuild as part of §5.2 or earlier.

---

## Sub-step plan (locked, will adjust per implementation friction)

| # | What | Risk | Verifier idea |
|---|---|---|---|
| 5.1 | ✅ DONE (this tick) — dep audit | low | implicit |
| 5.2 | Add geopandas + rasterio + mplstereonet to FastAPI pyproject.toml + rebuild image; verify imports | medium (container rebuild) | `docker exec georag-fastapi python -c 'import geopandas, rasterio, mplstereonet'` |
| 5.3 | `gold.drillhole_intervals_visual` migration + Dagster asset | low | row count + bbox validity per fixture |
| 5.4 | `gold.cross_section_panels` migration + Dagster asset | low | similar |
| 5.5 | `gold.structure_measurements_visual` migration + Dagster asset | low | similar |
| 5.6 | Strip log viz — FastAPI endpoint `/internal/v1/viz/strip_log` returning HTML (Plotly embed) | medium | smoke against 20-collar fixture |
| 5.7 | Cross-section viz — same shape | medium | smoke against 20-collar fixture |
| 5.8 | Stereonet viz — matplotlib + mplstereonet | medium | smoke against structures fixture |
| 5.9 | Chart export contract enforcement (master plan §17.4) | medium | need to read §17.4 first |
| 5.10 | Drillhole Visual QA Agent (Pydantic AI) | medium | mock test |
| 5.11 | Visual Readiness Agent (Pydantic AI) | medium | mock test |
| 5.12 | React Inertia frontend pages (drillhole detail) | high | feature test |
| 5.13 | Acceptance test against 20-collar corpus | low | done-test |

---

## Done test (from master plan)

> a drillhole array can be visualized as strip logs + cross-sections,
> with full provenance + chart export contract metadata, and the
> Visual Readiness Agent correctly explains when a visualization is
> or isn't possible.

---

## Hard constraints from prior work

- `silver.drill_traces` and `silver.surveys` already exist (v1.49).
  §5 builds gold visual layers on top; does NOT touch silver desurvey.
- Test fixture: 20 collars under `019d74a1-fba8-7165-9ae6-a5bf93eef97d`
  (per `project_phase18_31_autonomous_run.md` memory). PLS-* + XLS-24-*
  Diamond drill. Use this corpus for §5 smoke tests.
- Existing `silver.lithology_intervals` + `silver.assays` fixtures
  feed gold visual tables (per master plan §15-§17).
- Free-licensing rule: only MIT/BSD/Apache 2.0 deps. `mplstereonet`
  is MIT. ✅

---

## How the post-reset session should proceed

1. Read this kickoff + the scope proposal + the briefing doc
2. Run `bash scripts/phase3_master_plan_step8g_verify.sh` to confirm
   the manifest still has fresh entries (cascade should be sub-second)
3. Open doc-phase 71 = §5.2: add the missing deps + rebuild the
   FastAPI image. This is the riskiest step (container rebuild) so
   do it early. If the rebuild fails, halt + document.
4. Continue through §5.3, §5.4, §5.5 (schema migrations) which are
   low-risk + don't need rebuilds
5. §5.6-5.8 (visualizations) — need the rebuild done
6. §5.9 (chart export contract) — first read master plan §17.4 in
   `C:\Users\GeoRAG\Desktop\GeoRAG_master_plan_v2.4.2.md`
7. §5.10/§5.11 (agents) — follow Pydantic AI patterns in
   `app/agents/phase0/`
8. §5.12 (frontend) — follow Inertia v3 + React 19 patterns from
   `resources/js/Pages/Admin/IngestionReview.tsx` (doc-phase 58-61)

---

End of §5 kickoff. Post-reset session: keep going.
