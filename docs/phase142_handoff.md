## Doc-phase 142 handoff — Admin nav drawer

**Status:** Live + Vite rebuilt + Octane reloaded. **84/84 substrate verifier**.

## What landed

Kyle reported dashboards looked empty — turned out he was on
`/dashboard` and `/explorer`, not the four `/admin/...` surfaces.
There was no nav link from the main app to the admin surfaces (a
carry-over from doc-phase 131).

### Changes

1. **`app/Http/Middleware/HandleInertiaRequests.php`** — added
   `is_admin` to the `auth.user` shared prop so the frontend can
   conditionally render admin nav.

2. **`resources/js/types.ts`** — added `is_admin?: boolean` to the
   `AuthUser` interface.

3. **`resources/js/Layouts/AppLayout.tsx`** — added:
   - `ADMIN_NAV_ITEMS` constant with the 4 surfaces' href + label + description
   - Desktop nav: "Admin ▾" dropdown button (only renders for admins)
   - Mobile nav: "Admin" section header + flat list of links (only renders for admins)
   - Dropdown auto-closes on outside-focus or link click

The Admin button highlights when on any `/admin/*` route. Dropdown
items highlight the active surface.

### Visual layout

```
┌───────────────────────────────────────────────────────────────┐
│ GeoRAG Intelligence v1.0  [Dashboard] [Chat] [Search]         │
│  [Explorer] [Analytics] [Public Geoscience] [New Project]     │
│  [Admin ▾]                                                    │
│   └──┐                                                         │
│      │ ADMIN SURFACES                                         │
│      │ Eval Dashboard                                         │
│      │   Golden questions + run summaries                     │
│      │ Decision History                                       │
│      │   §21 decision records + audit anchors                 │
│      │ Support Cockpit                                        │
│      │   §25 tickets + triage/investigation chain             │
│      │ Hypothesis Workspace                                   │
│      │   §9.10 competing hypotheses register                  │
└──────┴────────────────────────────────────────────────────────┘
```

## Smoke verification

```bash
# Pint passes
vendor/bin/pint --dirty --format agent
# → {"tool":"pint","result":"passed"}

# Vite rebuilt
npm run build
# → app-DPJrZMti.js bundled (639.29 kB / 203.21 kB gzip)

# Octane reloaded so the new HandleInertiaRequests share() fires
php artisan octane:reload

# Substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 84/84 checks passed
```

## How Kyle sees it

After hard-refresh in browser, the top nav on every authenticated
page shows the **Admin** dropdown (right-most nav item). Clicking it
reveals all 4 surfaces with their descriptions. Each surface already
has real data populated from the 132→141 ticks:

| Surface | Real data |
|---|---|
| Eval Dashboard | 4 runs, 115 result rows, 45 active golden questions |
| Decision History | 2 decisions, 536 decision.* audit anchors |
| Support Cockpit | 6 tickets fully triaged + investigated + packeted |
| Hypothesis Workspace | 9 hypotheses, 27 evidence links |

## Cumulative session state

- **Doc-phase ticks this run:** 142
- **Admin nav drawer:** ✅ live
- **Live pytest cases:** 134 (no change — frontend-only tick)
- **Substrate verifier:** **84/84 PASS**

## Carry-overs

- Admin nav uses inline dropdown — no shadcn yet. When shadcn lands
  the dropdown can swap to `<DropdownMenu>` for accessibility +
  keyboard-nav improvements.
- The dropdown's `onBlur` handler is a known good-enough pattern but
  doesn't handle the "click-outside-to-close" case for
  non-keyboard users (it closes when focus leaves the menu region).
  A real outside-click detector would be cleaner.
