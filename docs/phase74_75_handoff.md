## Doc-phase 74-75 handoff — §6 scope proposal + §6.1 audit + §6.4 boundary agent skeleton

**Status:** Both ticks complete. Combined handoff (analogous to 72-73) to
stay efficient on context across the autonomous push.

## Doc-phase 74: master-plan §6 scope proposal

`docs/master_plan_section6_scope_proposal.md` — counterpart to §5 scope
proposal. Reads master-plan §6 deliverables against current v1.49
codebase and breaks the work into 15 sub-steps (6.1–6.15).

Key findings:
- v1.49 already populated `public_geoscience.*` with most Saskatchewan
  public sources (audit in §6.1 below confirmed 29 tables).
- §6 is ~60% frontend, ~40% backend. Inverse of §5.
- Backend ticks safe for autonomous run: 6.1 (audit), 6.4 (Public/Private
  Boundary Agent), 6.5 (saved map views table), 6.6 (h3-pg aggregations).
- Frontend ticks (6.7–6.14) should wait for Kyle — Inertia React +
  MapLibre product-feel decisions.
- 4 open questions tabled for Kyle: §6 vs §5 ordering, BC MINFILE/NRCan
  priority, saved-views auth model, h3 resolutions.

## Doc-phase 75: §6.1 audit + §6.4 Public/Private Boundary Agent skeleton

### §6.1 — public_geoscience.* schema audit

29 tables found in `public_geoscience.*`:
- 9 base `pg_*` tables (assessment_survey, bedrock_geology,
  drillhole_collar, mine, mineral_disposition, …)
- 9 corresponding `_history` audit tables (SCD2 pattern)
- 8 MVT view functions for Martin tile server
- 3 reference tables (`commodity_aliases`, `jurisdictions`, `sources`)

This is significant existing infrastructure. The master-plan §6 line
"all Saskatchewan public sources verified" is largely DONE in v1.49
baseline. Only **BC MINFILE** + **NRCan/GEO.ca** need new ingestion
flows (sub-steps 6.2, 6.3).

### §6.4 — Public/Private Boundary Agent

New module `app/agents/phase6/`:
- `public_private_boundary.py` — `@georag_agent`-decorated; risk_tier
  **R2** (one tier above R1 — this agent *blocks* answer emission); takes
  `workspace_id`, `retrieved_chunks`, `candidate_response_text`; returns
  `tagged_chunks` + `language_violations` + `approve_for_emission`.
  Skeleton (NotImplementedError body).
- `__init__.py` — re-exports the agent.

Per master-plan §2.9, this is THE regulatory anchor. Every retrieval
runs through it; every chunk gets `data_visibility: public | workspace`;
every candidate response is validated against the §2.9 template
("Public records show… within 25 km of project area. The private
project corpus does not yet include…").

Import smoke-tested in the running `georag-fastapi` container after
sync to the bind-mount path `/home/georag/projects/georag/src/fastapi`:

    docker exec georag-fastapi python -c \
      "from app.agents.phase6 import public_private_boundary; print(...)"
    => <function public_private_boundary at 0x...>

## Why skeleton not implementation

The agent needs two callers that don't exist yet:
1. **LangGraph Answer Graph** — §6's retrieval pipeline integration
   point; lands in later §6 ticks.
2. **Chat retrieval pipeline** — currently routes through §4's RAG
   chain which doesn't yet emit `data_visibility` tags on chunks.

Skeleton-now-implement-later: same pattern as doc-phases 49 (OCR
skeletons), 73 (§5.10/§5.11 visual agents). Interface contract locked;
behavior lands when callers exist.

## Master-plan §6 progress

| Sub-step | Status |
|---|---|
| 6.1 audit existing public_geoscience.* | ✅ DONE |
| 6.2 BC MINFILE ingestion | pending (medium backend) |
| 6.3 NRCan/GEO.ca ingestion | pending (medium backend) |
| 6.4 Public/Private Boundary Agent | ✅ skeleton |
| 6.5 Saved map views table | pending (small backend) |
| 6.6 h3-pg density aggregations | pending (medium backend) |
| 6.7-6.14 frontend layer packs + MapLibre work | pending (waits for Kyle) |
| 6.15 acceptance test | pending |

**2 of 15 sub-steps closed.** Next autonomous-safe backend ticks: 6.5
(saved map views table — small migration similar to doc-phases 50/71)
and 6.6 (h3-pg density aggregations Dagster asset).

## Carry-overs for next ticks

1. **fastapi image rebuild** — still pending from §5; blocks §5.6–5.8.
2. **Dagster assets** for the 3 §5 gold tables — separate work.
3. **§6.5 saved map views table** — recommended next tick. Small DDL.
4. **§6.6 h3-pg density aggregations** — Dagster asset + materialized
   views; medium work.
5. **§6.4 implementation** waits on Answer Graph integration point.
6. **Kyle's 4 §6 open questions** — table from scope proposal doc, will
   surface at 8am pickup.

## Recommended next tick

Doc-phase 76 = §6.5 (Saved map views table). Pure backend; small DDL;
no image rebuild dependency; no frontend coupling. Pattern matches
doc-phase 50 (Laravel migration + apply-as-superuser workaround).
