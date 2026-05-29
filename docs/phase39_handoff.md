# Phase 39 Handoff — R-P11-B slice 1 (Search/Query skeleton)

**Document version:** 1.0
**Status:** Phase 39 slice 1 complete. R-P11-B (Search/Query frontend) opened with the skeleton page, route, and feature test.
**Predecessors:** `docs/phase38_handoff.md`, `docs/phase39_implementation_kickoff.md`.

---

## 1. What Phase 39 slice 1 delivered

The opening slice of R-P11-B — the long-deferred dedicated Search/Query
surface. Slice 1 ships a static skeleton: input + submit + empty result
area, wired to Inertia behind `auth:sanctum`. Backend submission lands
in slice 2 (Phase 40); see `docs/phase39_implementation_kickoff.md` for
the full 5-slice plan.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `resources/js/Pages/SearchQuery.tsx` — 69-line skeleton React page. Header + search input + submit button + empty result area + footer marker noting "Phase 39 skeleton". Uses AppLayout. No data fetching yet (slice 2 wires SSE). | `scripts/phase39_step1_verify.sh` (8/8) |
| 2 | `routes/web.php` — `GET /search` registered under the `auth:sanctum` group, name `search`, inline `Inertia::render('SearchQuery')`. Pattern mirrors `/chat` and `/explorer`. | (same) |
| 3 | `tests/Feature/SearchQueryPageTest.php` — 2 PHPUnit feature tests: guest redirected to `/login`, authenticated user sees Inertia component `SearchQuery`. No DB requirement (the skeleton has no server-side props). | (same) |
| 4 | `scripts/phase39_step1_verify.sh` — 8-check verifier: page presence + size, Head/AppLayout imports, form elements, slice-1 marker, route under auth:sanctum, Inertia component reference, test methods, test runs cleanly. | self |
| 5 | This handoff + master sweep | — |

---

## 2. Page layout (slice 1)

```
┌────────────────────────────────────────────┐
│ Search                                     │
│ Ask one question, get one cited answer.    │
│ For multi-turn exploration use chat.       │
│                                            │
│ ┌────────────────────────────────┬──────┐  │
│ │ e.g. What is the average...    │ Ask  │  │
│ └────────────────────────────────┴──────┘  │
│                                            │
│ ┌────────────────────────────────────────┐ │
│ │  Results will appear here once slice 2 │ │
│ │  wires the SSE submission.             │ │
│ └────────────────────────────────────────┘ │
│                                            │
│ Phase 39 skeleton — R-P11-B slice 1 of 5.  │
└────────────────────────────────────────────┘
```

The submit handler is `e.preventDefault()` only — slice 2 attaches the
`/internal/queries` SSE subscription.

---

## 3. Why no controller in slice 1

The kickoff doc mentioned a controller as part of the slice-1 scope,
but the existing convention for sibling user-facing pages (`/chat`,
`/explorer`) is inline `Inertia::render('PageName')` in `routes/web.php`.
Per CLAUDE.md ("Stick to existing directory structure") and the
session's no-premature-abstraction discipline, slice 1 follows the
inline pattern. When slice 2 needs server-side props (e.g. a list of
recent queries from history persistence) we can promote to a
`SearchQueryController@index` without renaming any routes — the route
name `search` stays the same.

---

## 4. Verifier shape

`scripts/phase39_step1_verify.sh` runs 8 checks:

1. Page file present and ≥ 40 lines.
2. Page imports `Head` from `@inertiajs/react` and references `AppLayout`.
3. Page has `<input>` (type="text") + `<button>` (type="submit").
4. Page contains the literal "Phase 39 skeleton" marker.
5. `/search` route registered under the `auth:sanctum` middleware group.
6. Route renders `Inertia::render('SearchQuery')`.
7. Test class has both `test_guest_is_redirected_to_login` and
   `test_authenticated_user_sees_search_page` methods.
8. The feature test class runs cleanly (passed or skipped) in the
   Octane container.

The two test-method names were chosen to read naturally and to match
PHPUnit's snake-case convention used in the other feature tests.

---

## 5. What's still pending in R-P11-B

| Slice | Phase | Scope |
|-------|------:|-------|
| 2 | 40 | SSE submission against `/internal/queries` (reuse Chat plumbing) |
| 3 | 41 | Citation display + EvidenceInspector reuse from `Chat.tsx` |
| 4 | 42 | History panel (last 10 queries in localStorage) + URL deep-link |
| 5 | 43 | Top-nav integration + placeholder cleanup |

Each slice independently green and small enough for one autonomous-loop
tick. The aggregate ~470 LOC stays under the multi-slice budget pattern
established by R-P15-1.

---

## 6. Files of record (slice 1)

```
resources/js/Pages/SearchQuery.tsx        — new (69 lines)
routes/web.php                            — +6 lines (route + comment)
tests/Feature/SearchQueryPageTest.php     — new (38 lines)
scripts/phase39_step1_verify.sh           — new (verifier)
docs/phase39_implementation_kickoff.md    — kickoff (Phase 37/38 era)
docs/phase39_handoff.md                   — this file
scripts/phase39_master_sweep.sh           — sweep
```

No backend changes. No new dependencies. No new layout primitives.

---

## 7. Open carry-overs beyond R-P11-B

After R-P11-B slices 2-5 land, the major-shape backlog is empty:
- R-P14-3.5 ✅ closed Phase 19
- R-P14-3.4 ✅ closed Phase 20
- R-P14-3.7 ✅ closed Phase 21
- R-P21 ✅ closed Phase 38 (SQL → endpoint → page)
- R-P15-1 ✅ closed Phase 36 (4-slice prompt migration)
- R-P11-B ⏳ slice 1/5 (this phase)

The Phase 39+ run continues the autonomous-loop cadence established
since Phase 18.

End of Phase 39 handoff.
