## Doc-phase 129 handoff — §9.12 Decision History admin view + RLS retrofit

**Status:** Live. **Substrate verifier 72/72** + **72 live pytest cases pass**.

## What landed

### Migration — RLS admin-escape-hatch retrofit (9 policies)

`database/migrations/2026_05_13_160000_retrofit_rls_admin_escape_hatch.php`

The pattern was established in doc-phase 50 for older `silver.*` tables
(`USING (... OR GUC IS NULL OR GUC = '')`). My doc-phase 76+ tables
shipped without the escape hatch, which blocked admin cross-workspace
queries. Retrofitted 9 policies:

- `silver.saved_map_views` (doc-phase 76)
- `silver.hypotheses` (doc-phase 91)
- `silver.decision_records` (doc-phase 92)
- `silver.source_trust_scores` (doc-phase 102)
- `targeting.target_candidate_zones`
- `targeting.target_scores`
- `targeting.target_recommendations`
- `targeting.target_review_decisions`
- `targeting.target_outcomes` (doc-phase 85)

EXISTS-based child policies (decision_evidence_links / options /
outcomes / lessons_learned, hypothesis_evidence_links,
source_trust_features, target_score_factors, target_uncertainties)
gain the escape hatch transitively through their parent — no change
needed.

Pattern (verbatim):
```sql
USING (
    (workspace_id::text = current_setting('app.workspace_id', true))
    OR current_setting('app.workspace_id', true) IS NULL
    OR current_setting('app.workspace_id', true) = ''
)
WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true))
```

Note WITH CHECK still requires a workspace_id match — writes are still
strictly enforced. Only READS get the cross-workspace admin lens.

### Decision History controller

`app/Http/Controllers/Admin/DecisionHistoryController.php`:

5 query helpers + `index()` returning `Inertia::render('Admin/DecisionHistory', [...])`:

1. **kpis** — total / audit-anchored / audit_anchor_pct / mean_uncertainty
   / distinct_workspaces / distinct_deciders / recent_30d_count /
   latest_decided_at
2. **byDecisionType** — per-type rollup with human_decision breakdown
3. **byHumanDecision** — cross-type rollup (accepted/modified/rejected/
   signed_off/other)
4. **recentDecisions** — last 50 decisions, filter-aware (decision_type +
   workspace_id)
5. **recentAuditAnchors** — last 100 `audit.audit_ledger` rows where
   `action_type LIKE 'decision.%'`

### Route

`GET /admin/decision-history` → `admin.decision-history` name.

### React page — `resources/js/Pages/Admin/DecisionHistory.tsx`

~450 lines following the EvalDashboard pattern. Sections:

1. **Header** + back-link + cross-link to Eval Dashboard
2. **KPI tiles** — 4 cards. `audit_anchor_pct` tile turns red <90%,
   amber 90-99%, emerald at 100% (§29.2 export-compliance checklist
   target = 100%)
3. **Filter strip** — 9 chips (1 "all" + 8 decision_types) + active
   workspace filter chip with ✕ clear button. Uses `router.get()` with
   `preserveScroll/preserveState` for in-place URL updates.
4. **Per-decision-type table** — 7 cols, color-coded by outcome
5. **Side-by-side: human_decision rollup + quality signals card** —
   mean_uncertainty, audit_anchor_pct (colored), distinct_deciders,
   latest_decided_at + a contextual note about §29.2
6. **Recent decisions (50)** — click workspace/type to filter; ✓/✗ for
   audit_anchor; uncertainty as right-aligned mono; recommendation
   truncated to 200 chars
7. **Recent audit anchors (100)** — last 100 `decision.*` action_type
   rows; cross-references back to decisions via target_id

### Today's live data shown

| Section | Value |
|---|---|
| Total decisions | 0 |
| Audit anchors | **100** (from pytest test runs — decisions cleaned up, ledger preserved per immutability) |
| Distinct workspaces | 0 |
| `byDecisionType` rows | 0 |
| `recentDecisions` rows | 0 |

Empty-state UI handles each section. As soon as the §9.10 capture
hooks wire up (when a Laravel sign-off action fires `record_decision`),
the dashboard fills in automatically.

### Smoke verification

```
docker exec georag-laravel-octane php artisan tinker --execute '...'
# → controller OK

docker exec georag-laravel-octane php artisan route:list --json | grep decision-history
# → admin/decision-history

docker exec georag-laravel-octane php /tmp/dec_smoke.php
# kpis: OK — total=0, anchored=0 (0%), workspaces=0
# byDecisionType: OK — 0 rows
# byHumanDecision: OK — 0 rows
# recentAuditAnchors: OK — 100 rows  ← real audit-ledger data
# recentDecisions (no filter): OK — 0 rows

vendor/bin/pint --dirty --format agent
# → {"tool":"pint","result":"passed"}
```

### Combined live-pytest tally

72 live pytest cases across 10 modules all pass in 6.27s:

| Module | Cases |
|---|---|
| test_ontology_resolver | 10 |
| test_decision_recorder | 5 |
| test_support_access_audit | 4 |
| test_hash_chain_proof | 5 |
| test_langfuse_link | 6 |
| test_decision_summary | 6 |
| test_ontology_stats | 8 |
| test_workspace_audit_excerpt | 8 |
| test_mechanical_questions | 14 |
| test_sme_seeders | 6 |
| **Total** | **72** |

## Cumulative session state

- **Doc-phase ticks this run:** 129
- **Live helpers:** 8 + 2 admin dashboards (Eval, Decision History)
- **Live pytest cases:** 72 — all green
- **Substrate verifier:** 72/72 PASS
- **Master plan §9.12 backend:** complete enough to serve a real
  cross-workspace admin view; UI is live with empty-state handling for
  every section
- **Production data preserved:** 45 mechanical golden questions still
  active in eval.golden_questions; SavedMapView smoke test still green

## Next ticks

The Decision History view becomes meaningful once `record_decision` is
called from real production flows. Three productive directions:

1. **Wire `record_decision` into a real flow** — e.g., the §10.10
   `support_replay` workflow could log a `decision.workflow_enablement`
   when an ops user dry-runs a workflow.
2. **Next frontend surface** — Support Cockpit (§10.11). Mirrors the
   pattern again; backend has ops.support_tickets ready.
3. **Skeleton graduation** — `evaluate_workspace` task body, so the Eval
   Dashboard's "Recent runs" section fills in.

## Carry-overs

- `npm run build` needed before either dashboard renders in browser.
- Verifier currently doesn't run an Inertia route-smoke check against
  the dashboards. Could add but rendering needs a built Inertia bundle.
- `silver.decision_records` retrofit also enables future per-workspace
  Decision History surfaces (set `app.workspace_id` GUC at request
  middleware time) without code changes.
