## Doc-phase 104 handoff — §10.13 LangFuse link + §11.5 Tenant Isolation CI + §11.4 DR runbook scaffolds

**Status:** Complete. 7 deliverables across 3 sub-steps.

## What landed

### §10.13 — LangFuse trace replay link integration

`src/fastapi/app/services/support_cockpit/langfuse_link.py`:
- **`build_langfuse_trace_url(trace_id, *, base_url=None)` — pure
  function, working today.** Reads LANGFUSE_BASE_URL env var with
  `http://langfuse:3000` fallback for in-cluster.
- `open_trace_with_audit(conn, *, trace_id, workspace_id,
  ops_user_id, ticket_id)` — async; combines URL build with the
  §10.12 audit emission. Skeleton (waits on §10.12 graduation).

Re-exported from `app.services.support_cockpit`. Smoke-tested:

    >>> build_langfuse_trace_url("abc123", base_url="https://langfuse.example.com")
    'https://langfuse.example.com/trace/abc123'
    >>> build_langfuse_trace_url("xyz789")
    'http://langfuse:3000/trace/xyz789'

§10 is now **11 of 14 sub-steps closed (79%)**.

### §11.5 — Tenant Isolation Auditor CI workflow stub

`.github/workflows/tenant-isolation-auditor.yml`:
- Triggers on PRs touching migrations, fastapi app, Laravel app, or
  the workflow itself.
- postgis/postgis:18-3.6 service container matching production.
- Skeleton job that announces what the real auditor will check + exits 0
  to keep CI green until §11.5 implementation lands.
- TODO sections call out the 5 checks (apply migrations → seed 3
  synthetic workspaces → assert single-workspace SELECT → assert
  cross-workspace SELECT empty → assert WITH CHECK guards block
  cross-workspace mutations).

§11 is now **4 of 12 sub-steps closed (33%)**.

### §11.4 — 5 DR runbook scaffolds

`ops/runbooks/dr-1-postgres-loss.md` — Postgres data loss
`ops/runbooks/dr-2-store-divergence.md` — Cross-store divergence
`ops/runbooks/dr-3-ransomware.md` — Ransomware / data tampering
`ops/runbooks/dr-4-full-datacenter.md` — Full region loss
`ops/runbooks/dr-5-partial-outage.md` — Partial-outage degraded mode

Each runbook:
- Scope statement (what's in/out)
- Detection signals
- RTO/RPO placeholders (Kyle fills final numbers)
- Phased procedure (Triage → Restore → Reconcile → Verify)
- "Open questions for Kyle" section

Cross-reference: dr-1 references §11.3 restore_workspace
(doc-phase 100); dr-2 references the outbox pattern from Phase 0;
dr-3 references the audit-chain verifier (§22 / doc-phase 0); dr-4
references §11.6 + §11.7 deployment topology; dr-5 lists per-
subsystem degradation strategies.

These are scaffolds, not final runbooks. Kyle's §11.4 graduation
fills in the procedural detail + locks RTO/RPO numbers + operator
contact info.

§11 is now **5 of 12 sub-steps closed (42%)**.

## Master-plan progress map

| Phase | Status |
|---|---|
| §5 | scope + substrate done; viz endpoints wait |
| §6 | scope + 3 sub-steps closed |
| §7 | scope + 10/16 (62%) |
| §8 | scope + 6/14 (43%) |
| §9 | scope + 12/14 (86%) |
| §10 | scope + **11/14 (79%)** ← +1 this tick |
| §11 | scope + **5/12 (42%)** ← +2 this tick |
| §12 | scope + 11/13 (85%) |

Net: ~80 of ~111 sub-steps closed at autonomous-safe scaffolding
level (~72%).

## Recommended next ticks

Autonomous-safe ground genuinely shrinking. Remaining options:
- Frontend scaffolding stubs (Inertia React pages with placeholder
  content) — borderline product-feel; usually waits for Kyle.
- §9.12 backend API endpoints for the data lineage graph UI
  (separate from the React frontend).
- §6.5 SavedMapView Eloquent model + factory + tests (Laravel-side
  model layer for the doc-phase 76 table).
- §6.5 Laravel CRUD controller for SavedMapView.

Doc-phase 105 = §6.5 Laravel model layer + controller skeleton.
Final autonomous-safe backend tick for §6. After that the autonomous
run is genuinely exhausted.

## Carry-overs

Unchanged plus:
- LangFuse base URL is configurable via LANGFUSE_BASE_URL env var.
  Confirm prod value when Cockpit lands.
- Tenant Isolation Auditor implementation (the actual test code in
  `tests/tenant_isolation/`) lands as part of §11.5 graduation.
- DR runbook RTO/RPO numbers tabled for Kyle.
