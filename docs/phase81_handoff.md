## Doc-phase 81 handoff — §7.3-7.6 remaining in-graph agent skeletons

**Status:** Complete. 7 new agent skeletons + updated `__init__.py`. All 8 §7
in-graph agents now skeleton-callable.

## What landed

Seven new files under `app/agents/phase7/`:

| Agent | File | §15.4 role | Risk tier |
|---|---|---|---|
| Report Planner | `report_planner.py` | section structure + retrieval plan | R1 |
| Evidence Curator | `evidence_curator.py` | per-section retrieval + ranking | R1 |
| Claim Validator | `claim_validator.py` | §04i 6-layer validation | R1 |
| Map/Chart Planner | `map_chart_planner.py` | invokes viz subgraphs | R2 |
| Appendix Builder | `appendix_builder.py` | manifests + hash chain proof | R2 |
| Presentation Coach | `presentation_coach.py` | workspace-tone rewrite | R1 |
| Conflict Resolver | `conflict_resolver.py` | §29.2 conflict disclosure | R1 |

Plus `export_compliance.py` from doc-phase 78 (R3).

Each agent:
- `@georag_agent`-decorated; signature locked; raises NotImplementedError.
- Docstring includes the §15.4 role, output contract schema, and
  callsite expectations.
- Risk tier matches the agent's blast radius (R1 read-only, R2 SeaweedFS
  writer, R3 export-blocking).

`__init__.py` re-exports all 8 agents alphabetically.

### Risk-tier distribution

- **5 × R1** — Report Planner, Evidence Curator, Claim Validator,
  Presentation Coach, Conflict Resolver. All read-only or advisory-
  output; no idempotency key required.
- **2 × R2** — Map/Chart Planner, Appendix Builder. Both write
  rendered artifacts to SeaweedFS; idempotency keyed on (workspace_id,
  document_id) per `wrapper.py:161`.
- **1 × R3** — Export Compliance. Blocks export delivery;
  idempotency keyed on (workspace_id, export_request_id).

### Smoke test

    docker exec georag-fastapi python -c "
        from app.agents.phase7 import (
            appendix_builder, claim_validator, conflict_resolver, evidence_curator,
            export_compliance, map_chart_planner, presentation_coach, report_planner,
        )
    "
    => all 8 import + callable

## Master-plan §7 progress

| Sub-step | Status |
|---|---|
| 7.0 scope proposal | ✅ DONE |
| 7.1 Report Builder Graph skeleton | ✅ state + node stubs |
| 7.2 Eleven report-type templates | pending |
| 7.3 Report Planner + Evidence Curator | ✅ skeletons |
| 7.4 Claim Validator + Conflict Resolver | ✅ skeletons |
| 7.5 Map/Chart Planner | ✅ skeleton |
| 7.6 Appendix Builder + Presentation Coach | ✅ skeletons |
| 7.7 Hash chain proof JSON | ✅ skeleton |
| 7.8 Export Compliance Agent | ✅ skeleton |
| 7.9 PDF/DOCX/XLSX renderers | pending (image rebuild) |
| 7.10 generate_report Hatchet workflow | pending |
| 7.11 Activepieces delivery | pending |
| 7.12-7.15 22 dashboards | pending (waits for Kyle) |
| 7.16 TDD acceptance test | pending |

**8 of 16 §7 sub-steps closed** (plus scope proposal). All R3-path
agents now skeleton-callable. Half the phase closed via
skeleton-pattern; remaining ticks are either image-rebuild-blocked
(7.9) or frontend (7.12-7.15) or templates (7.2 — could land
autonomously).

## Recommended next tick

Doc-phase 82 = §7.2 (eleven report-type template manifests). Pure JSON
config; no image rebuild, no frontend, no agent invocation needed.
Each template manifest defines `{report_type, sections[], risk_tier,
required_evidence_kinds}`. Even the four manual R5 types (TDD,
NI 43-101, CSA 11-348, Data Room) can land as section-skeleton JSON
without QP-sign-off integration — the manifests just lock the
section-structure contract.

Alternative: doc-phase 82 = §7.10 generate_report Hatchet workflow
skeleton. Pattern matches `ingest_pdf` + `re_ocr_page` workflows.

Will pick at next tick. Templates are higher-leverage (unlocks the
graph for any future single-section dry-run); Hatchet workflow is
the eventual integrating glue.

## Carry-overs

1. **Unified image rebuild** stack: §5 geopandas/rasterio/mplstereonet
   + §7 weasyprint/python-docx/openpyxl + langgraph extra. Bundle.
2. **Conflict Resolution Graph** — `conflict_resolver` agent docstring
   names it as a dependency. Doesn't exist yet as a module; will need
   its own scope discussion when §7 graduates from skeleton.
3. **Workspace tone setting** — `presentation_coach` reads a workspace
   `report_tone` (technical/executive/regulator) setting. Needs adding
   to `silver.workspaces` schema in a later tick.
