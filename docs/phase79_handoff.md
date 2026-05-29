## Doc-phase 79 handoff — §7.7 hash chain proof JSON skeleton

**Status:** Complete. Skeleton + output contract locked + import smoke-tested.

## What landed

`src/fastapi/app/audit/hash_chain_proof.py` — new module alongside
the existing `app.audit.emit_audit` emitter. Single async function
`build_hash_chain_proof(conn, *, report_id, workspace_id, start, end)`
that returns the §15.3 `hash_chain_proof.json` payload for a report
bundle.

Output JSON contract locked in module docstring:
- `report_id`, `workspace_id`, `recipe_version` (= "v1", per the §22
  audit_ledger_hash_recipe Phase 11 hardening note)
- `verification_range: {start, end}` iso8601 UTC
- `rows[]` — every audit_ledger row touching the report. Each row
  carries id, created_at, action_type, actor_kind, target fields,
  payload_text, previous_hash_hex, stored_hash_hex, recomputed_hash_hex,
  match bool.
- `summary: {row_count, all_match, broken_ids[]}`

Skeleton (raises NotImplementedError); live implementation lands when
§7.1 Report Builder Graph reaches the `build_appendix` step.

### Why audit/ not services/

`app.audit` already houses `emit_audit` — the writer side of the
ledger. `build_hash_chain_proof` is the reader/proof side. Same
module = single home for ledger-aware code.

### Recipe alignment

The output JSON includes everything an external auditor needs to
re-run the recipe from `docs/audit_ledger_hash_recipe.md` without
GeoRAG code: previous_hash_hex, separators implied by the recipe doc,
payload_text as Postgres `jsonb::text`, created_at as iso8601
microsecond UTC. Match bool is precomputed for convenience but the
auditor can recompute and verify independently.

`RECIPE_VERSION = "v1"` is exported so future hardening (§22 item 1 —
RFC-8785 JCS migration) can bump it without breaking old proofs.

## Master-plan §7 progress

| Sub-step | Status |
|---|---|
| 7.0 scope proposal | ✅ DONE |
| 7.1 Report Builder Graph skeleton | pending |
| 7.2 Eleven report-type templates | pending |
| 7.3-7.6 4 in-graph agents | pending |
| 7.7 Hash chain proof JSON | ✅ skeleton |
| 7.8 Export Compliance Agent | ✅ skeleton |
| 7.9 PDF/DOCX/XLSX renderers | pending |
| 7.10 generate_report Hatchet workflow | pending |
| 7.11 Activepieces delivery | pending |
| 7.12-7.15 22 dashboards | pending (waits for Kyle) |
| 7.16 TDD acceptance test | pending |

**2 of 16 §7 sub-steps closed** (plus scope proposal).

## Recommended next tick

Doc-phase 80 = §7.1 Report Builder Graph state model. Define the
Pydantic `ReportBuilderState` shared across all 12 graph nodes from
§15.1. Plus stub LangGraph node functions for each (skeleton bodies).
This locks the §7-A v1 contract and makes the 8 in-graph agents
(Planner, Curator, Conflict Resolver, Claim Validator, Map/Chart,
Appendix Builder, Coach, Compliance) graph-callable when each
graduates from skeleton.

## Carry-overs

1. Report Builder Graph state model needs schema for: `report_id`,
   `report_type` (one of 11), `sections[]`, `evidence_ledger`,
   `citation_payload`, `sign_off_records`, `hash_chain_proof`. Will
   land in doc-phase 80.
2. The `silver.reports` table referenced in the docstring — verify
   existence at start of next tick. May need a migration.
