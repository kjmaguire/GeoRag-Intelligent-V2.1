## Doc-phase 83 handoff — §7.10 generate_report Hatchet workflow skeleton

**Status:** Complete. Workflow registered in AI pool; `worker --list` shows
`generate_report`. Task body NotImplementedError until graph nodes + image
rebuild graduate.

## What landed

- `src/fastapi/app/hatchet_workflows/generate_report.py` — new module
  following the `re_ocr_page` pattern:
  - `GenerateReportInput` Pydantic model (8 fields): workspace_id,
    project_id, report_type, requested_by_user_id, export_request_id,
    delivery_targets, report_window_start_iso, report_window_end_iso.
  - `GenerateReportOutput` Pydantic model (14 fields): report_id +
    success + 7 artifact URI slots + sign_off flags + failure metadata.
  - `generate_report = hatchet.workflow(...)` registration.
  - `@generate_report.task(execution_timeout="24h", retries=0)` body
    raises NotImplementedError.

- `src/fastapi/app/hatchet_workflows/worker.py` — registered
  `generate_report` in the **AI pool**. Choice rationale: reporting is
  AI/LLM work (drafts via LLM, evidence retrieval, claim validation),
  not ingestion. AI pool already houses agent workflows of similar
  shape (model_cost_summary_run, llm_incident_diagnosis_run, etc.).

### Why 24h timeout + retries=0

R4/R5 reports include `geologist_approval` node that pauses the
workflow for human sign-off. That pause can take hours or days
depending on operator availability. The 24h execution_timeout is
generous; production runs may need an even longer ceiling or Hatchet's
explicit pause/resume primitive.

`retries=0` because a report partway through generation should NEVER
silently re-run — the operator must explicitly retrigger. Idempotency
keying via `export_request_id` (R3-tier wrapper pattern from
`app/agents/wrapper.py:173-176`) means duplicate triggers with the
same export_request_id return the cached result.

### Smoke test

    docker exec georag-fastapi python -m app.hatchet_workflows.worker --list
    # … prints all workflow names including:
    generate_report

Workflow registration + import path clean.

## Master-plan §7 progress

| Sub-step | Status |
|---|---|
| 7.0 scope proposal | ✅ |
| 7.1 Report Builder Graph skeleton | ✅ state + node stubs |
| 7.2 Eleven report-type templates | ✅ |
| 7.3 Report Planner + Evidence Curator | ✅ skeletons |
| 7.4 Claim Validator + Conflict Resolver | ✅ skeletons |
| 7.5 Map/Chart Planner | ✅ skeleton |
| 7.6 Appendix Builder + Presentation Coach | ✅ skeletons |
| 7.7 Hash chain proof JSON | ✅ skeleton |
| 7.8 Export Compliance Agent | ✅ skeleton |
| 7.9 PDF/DOCX/XLSX renderers | pending (image rebuild) |
| 7.10 generate_report Hatchet workflow | ✅ skeleton + registered |
| 7.11 Activepieces delivery | pending |
| 7.12-7.15 22 dashboards | pending (waits for Kyle) |
| 7.16 TDD acceptance test | pending |

**10 of 16 §7 sub-steps closed.** The §7-A v1 backbone is fully
scaffolded — state, templates, graph nodes, in-graph agents, Hatchet
workflow, audit ledger proof builder, export compliance gate. The
phase is now blocked on:
- **image rebuild** (langgraph + weasyprint + python-docx + openpyxl)
- **frontend dashboards** (requires Kyle)
- **Activepieces install** (requires Kyle confirmation)

## Recommended next tick

Doc-phase 84 = open §8 (Target Recommendation Engine) scope proposal.
§7 autonomous-safe backend ticks are functionally done. §8 is the
next master-plan phase; scope proposal pattern matches §5/§6/§7.

The §8 deliverables per the master plan: 10 deposit model templates,
Athabasca uranium model fully populated, Target Recommendation Graph,
weighted-scoring engine, R5 sign-off, Target Pack map layer, Target
Recommendation Report template. Lots of backend skeleton work
amenable to autonomous push.

## Carry-overs

1. **Unified image rebuild** still pending — §5 + §7.9 + langgraph extra.
2. **R4/R5 sign-off Hatchet pause-resume** — the
   `geologist_approval` graph node needs Hatchet's pause/resume
   primitive. Verify pattern from existing `re_ocr_page` disposition
   flow at graph-wiring time.
3. **Outbox writes from `generate_report`** — final-state propagation
   to Reverb broadcast + downstream stores needs wiring when task body
   graduates.
