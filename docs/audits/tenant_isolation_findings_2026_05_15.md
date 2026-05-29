# Tenant Isolation Audit — 2026-05-15

**Auditor:** `src/fastapi/tests/test_tenant_isolation_auditor.py` (§11.5)
**Run date:** 2026-05-15
**Verdict:** **CLOSED — Blocks 1-4 shipped 2026-05-15. All 7 auditor gates pass strict; 0 xfails. Tenant isolation across silver/gold/audit/ops/workflow/targeting fully enforced.**

**Block 1 closure (2026-05-15)** — migration
`database/raw/phase0/96-rls-tenant-isolation-block1.sql` shipped.
silver.collars / reports / well_log_curves / hypothesis_evidence_links /
spatial_features now carry workspace_id (NOT NULL, FK to silver.workspaces,
B-tree index) with strict `workspace_id = current_setting('app.workspace_id')::uuid`
policies (no NULL-allows fallback). Gate 7 (cross-workspace SELECT probe) flipped
xfail → strict pass.

**Block 2 closure (2026-05-15)** — migration
`database/raw/phase0/97-rls-tenant-isolation-block2.sql` shipped. Sweeps
the remaining silver schema in three tiers:

- Tier B (RLS-only on tables already carrying workspace_id): silver.projects,
  kg_formation/mineral/report/sample_aliases, geological_formations,
  historic_workings, project_boundaries, collaboration_audit_log/comments/
  mentions/review_requests, drill_traces, review_queue
- Tier C (empty tables: full stack — column + FK CASCADE + idx + RLS):
  21 silver tables (pdf_*, agent_conversation*, exports, raster_layers,
  seismic_surveys, structures, surveys, alterations, decision_*, etc.)
- Tier D (small backfill via parent): silver.decision_options (4 rows via
  decision_records), silver.lithology_logs (4 rows via collars)

Shared reference data EXEMPTED in the auditor (not tenant-scoped by
design): silver.geological_ontology_terms / synonyms.

Downstream insert paths updated: decision_intelligence.recorder writes
workspace_id into decision_evidence_links / decision_options / decision_outcomes;
sync_silver_to_kg workflow + kg_sync now loop through silver.workspaces
to harvest project_ids under RLS before syncing to Neo4j.

After Block 2 the silver schema is fully workspace-scoped. The remaining
auditor xfails track audit/ops/workflow/targeting/public_geoscience
schemas (Blocks 3-4).

**Block 3 closure (2026-05-15)** — migrations
`database/raw/phase0/98-rls-tenant-isolation-block3.sql` +
`99-rls-block3-policy-tighten.sql` shipped. Covers audit / ops /
workflow / targeting / gold:

- audit.audit_ledger parent: tightened `tenant_isolation` policy.
  Reads: operator mode (no GUC) sees everything; tenant mode sees own
  workspace + system events. Writes: must match GUC or be system event.
  Cast wrapped in `NULLIF(..., '')` to avoid eager evaluation when GUC
  is empty (root-cause of a planner-order UUID-cast error).
- audit.audit_ledger_p* partitions: ENABLE + FORCE RLS per partition
  (PG doesn't auto-propagate RLS state from parent).
- audit.audit_ledger_verification_runs: add workspace_id + RLS.
- audit.integration_credentials_audit: enable RLS + policy + idx.
- workflow.workflow_runs parent: tightened (workspace_id IS NOT
  DISTINCT FROM NULLIF(GUC, '')::uuid). Partitions ENABLE+FORCE.
- ops.support_tickets / support_ticket_traces / support_replay_runs:
  add workspace_id (backfilled via support_tickets) + RLS + idx +
  GUC-pulling DEFAULT column expression.
- targeting.target_backtests + score_factors + uncertainties: full
  stack; missing indexes added on outcomes / recommendations /
  review_decisions / scores.
- gold.{drillhole_intervals_visual,cross_section_panels,
  structure_measurements_visual}: RLS + policy + idx.
- silver.answer_citation_spans: add workspace_id index.

Auditor exempts bumped: workflow.flow_jwt_keys / flow_registry
(platform infra), targeting.target_models / target_model_versions
(SME-curated global catalogue).

Auditor Gate 4 (workspace_id-filtering policy) updated to walk
pg_inherits up the partition tree so partitions inherit their parent's
policy in the check.

Downstream code/test updates:
- app/audit/__init__.py — emit_audit now sets/restores GUC around the
  INSERT so callers can write without pre-setting the workspace.
- 5 support_cockpit services (customer_response_drafting, escalation_
  routing, root_cause_investigation, support_packet, ticket_triage):
  set GUC to Default Workspace for the ticket lookup, then realign to
  the row's workspace.
- 2 Hatchet workflows (support_replay, what_changed_detector,
  restore_workspace): set GUC to workspace before scoped reads.
- 7 phase10 support test fixtures: conn fixture sets the GUC.

Remaining auditor xfails (5/5) now track ONLY public_geoscience
(21 tables) — Block 4 will address that schema after the SME decision
on shared-vs-scoped reference data is made.

**Block 4 closure (2026-05-15)** — migration
`database/raw/phase0/100-rls-tenant-isolation-block4.sql` shipped.

SME decision on public_geoscience: **shared open-data reference, NOT
tenant-scoped**. The schema holds Crown-copyright records from NRCan,
BC Geological Survey, SK Geological Survey, etc. — global data that
every workspace reads identically. Access control lives at the GRANT
level (georag_app has CRUD via role grants; non-georag_app roles get
SELECT only). Adding workspace_id columns there would force operators
to duplicate the open dataset per workspace for zero security gain.

The `public_geoscience` schema was REMOVED from `_TENANT_SCHEMAS` in
the auditor, eliminating all 21 offenders.

The Block 4 migration also added the silver.workspaces FK constraint
to the 99 tables that had workspace_id but were missing the FK:

- For most tables: strict FK with ON DELETE CASCADE.
- For audit.audit_ledger (4,013 rows pointing at workspaces deleted
  by test fixtures): FK added as NOT VALID — blocks new orphan rows
  while preserving the immutable audit history. VALIDATE CONSTRAINT
  can be run after a separate cleanup job.
- Partitioned-table FKs declared on the parent propagate to all
  partitions automatically (PG 13+).

Auditor changes:
- Gate 2 (FK check) rewritten to walk the partition tree via
  pg_inherits and use pg_catalog (not information_schema, which
  filters by role visibility and didn't show child FKs for the
  non-superuser test role).
- Gate 4 (policy check) similarly walks partition tree.

After Block 4 the auditor is fully green: 7 hard passes, 0 xfails.
The §11.5 Tenant Isolation Auditor work is complete.

The Tenant Isolation Auditor graduated in Phase H4 and immediately surfaced
real findings against the live schema. The detailed JSON output is at
`./tenant_isolation_findings_2026_05_15.json`. Headline numbers:

| Gate                                    | Offender count |
| --------------------------------------- | -------------- |
| Gate 1 — missing `workspace_id` column  | 60             |
| Gate 2 — `workspace_id` column with no FK to `silver.workspaces` | 64 |
| Gate 3 — RLS not enabled                | 72             |
| Gate 4 — RLS enabled but no `workspace_id`-filtering policy | 82 |
| Gate 5 — `workspace_id` not indexed     | 14             |
| Gate 7 — live RLS cross-workspace probe | **FAIL** (silver.collars) |

## Critical: silver.collars policy filters on `georag.project_id`

The RLS policy on `silver.collars` (`collars_project_scope`) filters on the
legacy single-tenant GUC `georag.project_id` and **allows when the GUC is
NULL**. This is an active cross-tenant leak primitive: any client that
fails to set the GUC will see every workspace's collars.

```sql
-- Current (broken):
USING (
  current_setting('georag.project_id', true) IS NULL
  OR project_id = current_setting('georag.project_id', true)::uuid
)

-- Required:
USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
```

Fix lands in a follow-up migration. Until then the Gate-7 assertion is
`xfail(strict=True)` so the auditor file stays green in CI but the defect
remains visible in the run report.

## Recommended remediation sequence

1. **Block 1 — silver.collars + 5 most-trafficked silver tables** (highest
   leakage risk):
   - Replace project-scoped policies with workspace-scoped policies
   - Add `workspace_id` column where missing + backfill
   - Add FK to `silver.workspaces`
   - Add `idx_<table>_workspace_id` B-tree

2. **Block 2 — remaining 56 silver/gold tables**: same pattern, migration
   per schema.

3. **Block 3 — audit / ops / workflow / targeting**: many are already
   workspace-scoped via FK on `workspace_id`, just need RLS enabled +
   policy added.

4. **Block 4 — public_geoscience**: re-confirm exemption decisions. The
   `pg_*` reference tables (jurisdictions, commodities, sources) are
   shared read-only by design and exempt. Most other `public_geoscience.*`
   are tenant-scoped derivatives and need full workspace policies.

Each block is a separate migration + a re-run of this auditor; the file
count above should monotonically decrease until all gates are green.

## Why the auditor xfails the schema-gap gates today

The five schema-gap gates would block every PR if asserted strictly while
the 60+-table remediation is in flight. The pragmatic compromise:

- Schema-gap gates (1, 2, 3, 4, 5) emit the offender list as a pytest
  `xfail(strict=True)` so the count is visible in CI but doesn't block
  merges.
- The active-defect gate (7 — RLS cross-workspace probe) is `xfail` until
  the silver.collars migration lands, then it MUST flip green.
- Gate 6 (settings validator refusing unsafe multi-tenant config) is a
  hard assertion — it tests application code, not schema state.

The xfail decorator carries a `reason=` pointing back to this file so the
findings stay discoverable.
