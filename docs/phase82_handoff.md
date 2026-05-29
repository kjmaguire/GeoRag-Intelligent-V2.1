## Doc-phase 82 handoff — §7.2 Eleven report-type template manifests

**Status:** Complete. All 11 templates load + queryable via
`get_template` / `get_risk_tier`. Import-smoked.

## What landed

`src/fastapi/app/services/report_builder/templates.py` — single
Python module containing all 11 §15.2 report-type manifests as
typed `dict[ReportType, list[SectionPlan]]`. Plus a sibling
`dict[ReportType, ReportRiskTier]` for sign-off requirements.

Public API:
- `REPORT_TEMPLATES: dict[ReportType, list[SectionPlan]]`
- `REPORT_RISK_TIERS: dict[ReportType, ReportRiskTier]`
- `get_template(report_type) -> list[SectionPlan]` — fresh shallow copy
- `get_risk_tier(report_type) -> ReportRiskTier`

Updated `app/services/report_builder/__init__.py` to re-export all four.

### Section breakdown

| Report type | Sections | Risk |
|---|---|---|
| weekly_project_digest | 4 | R3 |
| ingestion_quality | 4 | R3 |
| technical_due_diligence | 8 | R4 |
| executive_project_intelligence | 4 | R4 |
| gis_arcgis_sync | 3 | R3 |
| target_recommendation | 4 | R5 |
| public_geo_overlay | 4 | R3 |
| data_room_package | 4 | R5 |
| what_changed | 4 | R3 |
| ni43101_section_pack | 8 | R5 |
| csa11348_disclosure_pack | 4 | R5 |

**Total: 51 sections across 11 templates.**

NI 43-101 sections follow the regulatory item numbering (Items 3, 4, 6,
7, 10, 11, 12, 25 — the QP-attributed subset). TDD covers the
canonical exploration-report flow (Executive summary → Property
description → Regional/property geology → Exploration history →
Drilling → Data quality → Conclusions).

### Why one Python module instead of 11 JSON files

- **Typesafety**: each section is a `SectionPlan` Pydantic model;
  typos in template_slug / required_evidence_kinds are caught at
  module import.
- **Easier evolution**: single file = single review surface when the
  manifest grows or splits.
- **No runtime JSON loading cost** — templates are constants resolved
  at import.

If later a customer wants per-workspace template overrides, a JSON
overlay layer can read from `silver.workspace_report_templates` and
deep-merge over these defaults. Out of scope for §7.2.

### Smoke test

    docker exec georag-fastapi python -c "
        from app.services.report_builder import REPORT_TEMPLATES, get_template
        print(len(REPORT_TEMPLATES))   # 11
        print(len(get_template('ni43101_section_pack')))   # 8
    "

## Master-plan §7 progress

| Sub-step | Status |
|---|---|
| 7.0 scope proposal | ✅ DONE |
| 7.1 Report Builder Graph skeleton | ✅ |
| 7.2 Eleven report-type templates | ✅ DONE |
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

**9 of 16 §7 sub-steps closed.** Over half the phase locked at
skeleton / template level. Remaining ticks need image rebuild (7.9),
Hatchet glue (7.10), or are frontend (7.12-7.15).

## Recommended next tick

Doc-phase 83 = §7.10 generate_report Hatchet workflow skeleton. Last
backend-only autonomous-safe §7 tick before image rebuild blocks
further progress. Pattern matches `app/hatchet_workflows/ingest_pdf.py`
and `re_ocr_page.py`. Wires the report_builder graph nodes (still
skeletons) into a durable workflow with audit emission at start/end +
outbox writes + R4/R5 sign-off pause-resume.

After doc-phase 83, §7 backend autonomous-safe work is functionally
done. Next phase = §8 (Target Recommendation Engine) scope proposal.

## Carry-overs

1. **Unified image rebuild** stack still needed for §5 + §7.9 +
   langgraph extra. Hard blocker for any §7 graduation.
2. **Workspace template overrides** — not in scope for §7.2 but
   flagged as a potential later need.
3. Template `template_slug` values like `tdd.executive_summary` will
   key into a future markdown/prompt template store (separate from
   manifest structure) when the LLM draft step graduates.
