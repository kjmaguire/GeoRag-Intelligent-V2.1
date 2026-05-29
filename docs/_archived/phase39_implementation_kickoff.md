# Phase 39 Implementation Kickoff — R-P11-B Search/Query frontend

**Document version:** 1.0 — DRAFT.
**Status:** Active. Drafted during Phase 37 master sweep.
**Predecessors:** `docs/phase38_handoff.md`,
`docs/r-p15-1_prompt_migration_scope.md` (multi-slice migration shape — Phase 39+ follows the same cadence).

---

## 1. Theme

R-P11-B was named in the Phase 16 handoff and deferred repeatedly
through Phases 18–38 as out-of-scope for the autonomous run's
fixture / cache / agent focus. With R-P15-1 (Phase 36) and R-P21
(Phase 38) both closed end-to-end, R-P11-B is the last
major-shape carry-over.

Phase 39+ pursues it as a multi-slice migration (mirroring
R-P15-1's 4-slice cadence: each slice small enough for one
autonomous-loop tick, each leaves the suite at typical
30-31/31).

---

## 2. What exists today

```
resources/js/Pages/
├── Chat.tsx         (1538 lines — full conversational RAG surface)
├── Explorer.tsx     (321 lines  — 3-panel data explorer: DrillHoleBrowser + Map + StripLog)
├── Dashboard/       (Portfolio + Project + ProjectAnalytics)
├── Admin/           (HatchetWorkers, Integrations, WorkflowRuns, WorkspaceThresholds, CacheTelemetry as of P38)
└── ...
```

`Chat.tsx` is a multi-turn conversation surface: maintains
thread history, integrates Laravel Echo for streaming, surfaces
citations + evidence inspector. The R-P11-B carry-over is a
**separate** dedicated Search/Query page — a single-shot ask +
answer UX for the RAG pipeline without conversation overhead.

The two surfaces complement each other:
- **Chat** — exploratory, multi-turn, "let's iterate on this idea"
- **Search/Query** — single-shot, fast, "give me the answer to X"

---

## 3. Proposed multi-slice plan

| Slice | Phase | Scope | Estimated LOC |
|-------|------:|-------|--------------:|
| 1 | 39 | Skeleton page + route + controller + minimal AppLayout integration | ~120 |
| 2 | 40 | Query submission against the existing `/internal/queries` SSE endpoint (reuse the chat plumbing) | ~150 |
| 3 | 41 | Citation display + evidence inspector reuse (the existing components from Chat) | ~80 |
| 4 | 42 | History panel (last 10 queries persisted in localStorage) + URL deep-link to a specific query | ~100 |
| 5 | 43 | Cleanup: add to top nav; remove the temporary "search not implemented" placeholder anywhere | ~20 |

Each slice independent. Each lands a working incremental page.
Total estimated LOC: ~470 plus ~200 LOC of verifier scripts.

---

## 4. Done definition (Phase 39 slice 1)

- New `resources/js/Pages/SearchQuery.tsx` rendering an empty
  Search/Query shell — title, search input, submit button,
  empty result area, footer noting "Phase 39 skeleton".
- New `/search` route in `routes/web.php` rendering the page via
  Inertia, behind `auth:sanctum`.
- New feature test (PHPUnit) asserting:
  - guest → 302 redirect to /login
  - authenticated → 200 + Inertia page identifier present
- Verifier script asserts the structural changes + the test
  runs cleanly.

No backend agent integration yet — slice 2 wires the SSE
submission.

---

## 5. Why split this from Chat.tsx instead of extending it?

1. **Different UX contract.** Chat is conversation-shaped (state
   accumulates, threads matter). Search is single-shot (one
   question → one answer → repeat with a new query).
2. **Different page-state lifecycle.** Chat has WebSocket
   subscriptions, message history, thread switching. Search
   just needs an input + a result + maybe a short history list.
3. **Different navigation surface.** Chat lives at `/chat` (an
   ongoing conversation). Search lives at `/search` (a quick
   answer; bookmarkable by query string).
4. **Reusable components, not reused page.** Slice 3 will
   import `<ChatMessage>` / `<EvidenceInspector>` /
   `<CitationPGEODetail>` directly. Page composition, not page
   reuse.

---

## 6. Out of scope for Phase 39

- Backend changes (SSE endpoint already exists from Chat work).
- New chart libraries, design tokens, layout primitives.
- Search history persistence (slice 4 scope).
- "Suggested queries" or autocomplete (post-R-P11-B).

---

## 7. Files of record (preview, slice 1)

```
resources/js/Pages/SearchQuery.tsx       (slice 1 — skeleton page)
routes/web.php                            (slice 1 — /search route)
tests/Feature/SearchQueryPageTest.php    (slice 1 — feature test)
docs/phase39_implementation_kickoff.md   (this file)
docs/phase39_handoff.md                  (slice 1 close)
scripts/phase39_master_sweep.sh
scripts/phase39_step1_verify.sh
```

The Search/Query page lives at the top level (not under `Admin/`)
because it's user-facing, not admin telemetry.

End of Phase 39 kickoff (draft — apply after Phase 37 sweep clears).
