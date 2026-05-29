## Doc-phase 80 handoff — §7.1 Report Builder Graph state model + node stubs

**Status:** Complete. State model + 13 node stubs imported clean.

## What landed

New package `app/services/report_builder/`:
- `state.py` — Pydantic `ReportBuilderState` model (29 fields), plus
  `SectionPlan`, `EvidenceItem`, `Claim`, `SectionDraft`, `SignOffRecord`
  sub-models. `ReportType` enum with all 11 §15.2 types.
  `ReportRiskTier` enum (R3/R4/R5) drives sign-off requirements.
- `nodes.py` — 13 async node stubs (12 §15.1 nodes + 1 final delivery):
  `select_report_type → plan_sections → gather_evidence →
  verify_evidence_budget → generate_section_drafts → validate_claims →
  attach_citations → generate_maps_charts → build_appendix →
  compliance_check → geologist_approval → export_package →
  activepieces_delivery`. Each raises NotImplementedError; signatures
  + docstrings lock the contract.
- `__init__.py` — re-exports state + all 13 nodes.

### Why state model and node stubs, not LangGraph wiring

`langgraph` is only a pyproject **extra** (`uv sync --extra langgraph`)
— NOT installed in the runtime image. Trying to import `from
langgraph.graph import StateGraph` inside the container today returns
ModuleNotFoundError.

Skeleton-pattern application: lock the state shape + node contracts;
wire the graph when the image rebuild ships `langgraph`. Same pattern
as the §5 viz endpoints waiting on the geopandas/rasterio rebuild.

### Node-by-node design notes

Each node docstring captures its agent counterpart from §15.4 (e.g.,
`plan_sections` ↔ Report Planner Agent; `compliance_check` ↔
`app.agents.phase7.export_compliance`). The compliance_check stub
explicitly notes the delegation to §7.8. The build_appendix stub
explicitly notes the call to §7.7 (`build_hash_chain_proof`).

State telemetry fields (`started_at`, `completed_at`, `failure_reason`)
support the Hatchet workflow wrapping the graph (§7.10).

## Image-rebuild dependency stack

The image needs to absorb (in one rebuild):
- §5: geopandas, rasterio, mplstereonet
- §7: weasyprint (+ Pango/Cairo system deps), python-docx, openpyxl,
  langgraph + langgraph-checkpoint-postgres + langchain-mcp-adapters
  (the existing `[langgraph]` extra)

Recommended: bundle all of §5 + §7 deps into a single rebuild tick
when Kyle is available to monitor (image builds + container restart
+ smoke). Tracking as a unified carry-over.

## Master-plan §7 progress

| Sub-step | Status |
|---|---|
| 7.0 scope proposal | ✅ DONE |
| 7.1 Report Builder Graph skeleton | ✅ state + node stubs |
| 7.2 Eleven report-type templates | pending |
| 7.3-7.6 4 in-graph agents | pending (skeletons) |
| 7.7 Hash chain proof JSON | ✅ skeleton |
| 7.8 Export Compliance Agent | ✅ skeleton |
| 7.9 PDF/DOCX/XLSX renderers | pending (image rebuild) |
| 7.10 generate_report Hatchet workflow | pending |
| 7.11 Activepieces delivery | pending (Activepieces gate) |
| 7.12-7.15 22 dashboards | pending (waits for Kyle) |
| 7.16 TDD acceptance test | pending |

**3 of 16 §7 sub-steps closed** (plus scope proposal). All R3 paths
sketched at skeleton level.

## Recommended next tick

Doc-phase 81 = §7.3-7.4 (or one of) — Report Planner + Evidence Curator
agent skeletons in `app/agents/phase7/`. Same pattern as
`export_compliance`. Each is a thin `@georag_agent`-decorated function
with the §15.4 contract locked.

Alternatively, doc-phase 81 = §7.10 generate_report Hatchet workflow
skeleton. Pattern matches `ingest_pdf` + `re_ocr_page`. Wires the
report-builder graph nodes (when ready) into a durable workflow with
audit emission at start/end + outbox writes.

Will pick at next tick — agents are more granular and parallelizable
across ticks; Hatchet workflow is the integrating glue.

## Carry-overs

1. **Unified image rebuild** — §5 + §7 deps + langgraph extra.
2. **`silver.reports` table** — referenced in hash_chain_proof
   docstring; verify existence in next tick, may need migration.
3. **Activepieces install status** — gates §7.11 delivery node.
4. **R4/R5 sign-off Hatchet pause-resume contract** — `geologist_approval`
   node docstring promises this. Hatchet pause-resume primitive is
   used by `re_ocr_page` worker disposition flow already; same pattern.
