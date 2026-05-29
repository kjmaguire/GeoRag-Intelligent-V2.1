## Doc-phase 130 handoff — §10.11 / §25 Support Cockpit (third Track 3 surface)

**Status:** Live + smoke-verified. **72/72 substrate verifier**.

## What landed

### Controller — `app/Http/Controllers/Admin/SupportCockpitController.php`

Read-only Laravel controller mirroring the established Eval Dashboard
(doc-phase 128) and Decision History (doc-phase 129) patterns:
- `$this->authorize('admin')` gate
- Raw SQL via `DB::select(...)` / `DB::selectOne(...)`
- Returns `Inertia::render('Admin/SupportCockpit', [...])` with 10
  structured payloads:
  - **kpis** — 8 top-level counters (total/open/critical_open/unassigned_open
    tickets, resolved_30d, mean_resolution_hours, total_support_accesses_30d,
    latest_ticket_at)
  - **by_status** — open / investigating / resolved / closed counts
  - **by_severity** — critical / high / medium / low for non-closed tickets
  - **by_category** — wrong_answer / failed_ingestion / failed_report /
    integration_issue / performance / other
  - **recent_tickets** — last 50, filter-aware on status/severity/category
  - **recent_accesses** — last 100 `support_access` audit anchors
    (forensic trail of every cross-workspace ops access — emitted by
    `emit_support_access_audit` + `open_trace_with_audit`)
  - **recent_replays** — last 30 from `ops.support_replay_runs`
  - **filters** + 3 valid-value arrays for the filter strip

Filter validation: `VALID_STATUSES`, `VALID_SEVERITIES`,
`VALID_CATEGORIES` (plus `VALID_CHANNELS`, `VALID_ACCESS_KINDS`
held for future writer endpoints).

### Route — `routes/web.php`

`GET /admin/support-cockpit` → `admin.support-cockpit` name.

### React page — `resources/js/Pages/Admin/SupportCockpit.tsx`

~28 kB / ~500 lines. Matches the project's dark Tailwind palette
(`bg-stone-950` + `text-stone-100` + emerald/amber/red/sky accents).

Layout sections:
1. **KPI tiles row** — 4 cards: open tickets, critical open, unassigned
   open, accesses 30 d
2. **Filter strip** — 3 FilterRows for status, severity, category. Click
   to filter; `router.get` with `preserveScroll` + `preserveState`.
3. **Counts panels** — by_status / by_severity / by_category side-by-side
4. **Recent tickets** — table with severity + status badges; click row
   to filter on that workspace
5. **Recent support_access audits** — forensic trail with actor, workspace,
   target, access_kind, target_summary
6. **Recent replay runs** — last 30 from `ops.support_replay_runs`

Badge palette:
- Status: open=red, investigating=amber, resolved=emerald, closed=stone
- Severity: critical=bold red, high=red, medium=amber, low=sky

Empty-state handling on every section. When no tickets exist (today),
shows guidance: "The §10.11 in-app ticket form + the 5 §25.4 support
agents (ticket_triage, root_cause_investigation, support_packet,
customer_response_drafting, escalation_routing) will populate this
surface once they graduate from skeleton."

### What the dashboard shows TODAY (with real data)

| KPI | Value |
|---|---|
| Total tickets | 0 (no writer surface yet) |
| Open / critical open / unassigned open | 0 / 0 / 0 |
| **Support-access audits (30 d)** | **143** ← real audit anchors from pytest runs |
| Recent replay runs | 0 |

The 143 support_access audit anchors come from doc-phase 116
(`emit_support_access_audit`) + doc-phase 118 (`open_trace_with_audit`)
live helpers being exercised by the pytest suite. They form the
**actual forensic trail** that the Customer Support Cockpit was
designed to surface.

### Smoke verification

```bash
# Controller class loads
php artisan tinker --execute 'echo class_exists(SupportCockpitController::class)';
# → "OK"

# Route registered
php artisan route:list --path=admin/support-cockpit
# → admin/support-cockpit admin.support-cockpit registered

# All 7 controller data methods run end-to-end (via reflection bypass)
php /app/tmp/support_cockpit_smoke.php
# → kpis: OK — total_tickets=0, open=0, critical_open=0, accesses_30d=143
# → byStatus: OK (0 rows)
# → bySeverity: OK (0 rows)
# → byCategory: OK (0 rows)
# → recentTickets: OK (0 rows)
# → recentSupportAccesses: OK (100 rows)
# → recentReplays: OK (0 rows)

# Pint
vendor/bin/pint --dirty --format agent
# → {"tool":"pint","result":"passed"}

# Vite build
npm run build
# → public/build/assets/SupportCockpit-D18R2li1.js bundled

# Substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 72/72 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 130
- **Track 3 surfaces live:** Eval Dashboard, Decision History, Support Cockpit
- **Live helpers:** 8 + 3 admin surfaces
- **Live pytest cases:** 66
- **Substrate verifier:** **72/72 PASS**
- **Tracks closed:**
  - Track 1 (image rebuild): ✅ CLOSED through 4 builds
  - Track 2b (mechanical questions seed): ✅ 45 active in DB
  - **Track 3 (frontend surfaces):** ✅ 3 of 4 admin surfaces live
- **Tracks waiting for Kyle:**
  - Track 2a (§8.3 Athabasca SME content)
  - Track 3 follow-ons (Hypothesis Workspace, MapLibre layer packs)

## Recommended next ticks

The Support Cockpit works against real audit data today (143
support_access anchors). Three productive follow-ons:

1. **Verify in browser** — Kyle visits `/admin/support-cockpit`.
   Quick eyeball confirms the audit trail panel renders correctly
   and the filter UX is intuitive.
2. **Next frontend surface** — Hypothesis Workspace (§9.10) is the
   remaining Track 3 surface with a live aggregator behind it.
3. **Skeleton graduation** — either the §10.11 ticket-creation surface
   (writer side: form + controller) so tickets actually exist, or
   one of the 5 §25.4 support agents.

## Carry-overs

- The dashboard needs `npm run build` to actually serve — done.
- Inertia tests for `/admin/support-cockpit` (visiting + asserting
  prop shape) — not yet authored. Pattern matches the existing
  surfaces.
- All 3 Track 3 surfaces (Eval Dashboard, Decision History, Support
  Cockpit) share the same dark-Tailwind palette, KPI-tile-then-tables
  layout, and reflection-smoke pattern. The pattern is stable enough
  to scaffold the remaining surface (Hypothesis Workspace) quickly.
