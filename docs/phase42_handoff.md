# Phase 42 Handoff — R-P11-B slice 4 (history + URL deep-link)

**Document version:** 1.0
**Status:** Phase 42 slice 4 complete. Search/Query page now persists recent queries to localStorage and round-trips through `?q=…` deep-links.
**Predecessors:** `docs/phase41_handoff.md`.

---

## 1. What Phase 42 slice 4 delivered

Slice 4 of R-P11-B closes the bookmarkability loop. The page now:

- Persists the last 10 queries to `localStorage` under
  `georag.search.history.v1` (deduplicated; newest first).
- Reads `?q=…` on mount and auto-submits, so `/search?q=…` is a
  bookmarkable deep-link.
- Reflects the active query into the address bar with
  `history.replaceState` (no spurious back-button entries).
- Renders a "Recent queries" panel with click-to-rerun and a
  Clear control.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `resources/js/Pages/SearchQuery.tsx` — added `HistoryEntry` type, `loadHistory` / `saveHistory` / `pushHistory` helpers, a `useEffect` mount hook that hydrates history + handles `?q=…` auto-submit, refactored `handleSubmit` into a `runQuery(text)` `useCallback` so history rerun + deep-link can call it without going through the form, and added the "Recent queries" section + Clear button. | `scripts/phase42_step1_verify.sh` (8/8) |
| 2 | This handoff + master sweep | — |

---

## 2. localStorage shape

Key: `georag.search.history.v1` (versioned so a future migration can
bump the suffix without colliding).

Value: JSON array, newest first, capped to 10 entries.

```json
[
    {"query": "What is the average gold grade at Triple R?",
     "asked_at": "2026-05-12T12:00:01.234Z"},
    {"query": "List all assays for PLS-22-08",
     "asked_at": "2026-05-12T11:58:14.512Z"}
]
```

`loadHistory` validates each entry's shape before returning — a
corrupted payload from a previous version (or hand-edited
localStorage) degrades to an empty array rather than throwing.
`saveHistory` also wraps in try/catch since Safari private mode
can disable `localStorage` entirely.

---

## 3. Deep-link contract

| URL | Behaviour |
|-----|-----------|
| `/search` | Empty input; idle state. |
| `/search?q=foo` | Mount auto-fills the input with "foo" and submits. |
| User edits input + submits "bar" | URL becomes `/search?q=bar` via `replaceState`. |
| User clicks a history entry "baz" | URL becomes `/search?q=baz`. |

Auto-submit is gated by `autoSubmitRef` so React-strict-mode's
double-effect doesn't fire the handshake twice.

---

## 4. Why a useCallback refactor

Slice 1-3 had `handleSubmit(e)` as the single entry point. Slice 4
needs three callers:

1. The form's `onSubmit`.
2. The mount-time `?q=…` auto-submit.
3. The history-entry click handler.

Lifting the body into `runQuery(text)` (`useCallback` with empty
deps — no external closures) lets each caller share the exact same
state-reset → fetch → subscribe → start path. `handleSubmit` is now
a 3-line wrapper that just unwraps the form event.

---

## 5. Why a versioned storage key

`georag.search.history.v1` rather than `georag.search.history` —
matches the `prompts/` `PROMPT_VERSION` pattern. If slice 4's
storage shape needs to change later (e.g. adding result snapshots
for offline display), `v2` keeps the old payload around for a
migration step rather than silently dropping it.

---

## 6. Verifier shape

`scripts/phase42_step1_verify.sh` runs 8 checks:

1. Page references `localStorage` with the versioned history key.
2. History capped at exactly 10 entries (`SEARCH_HISTORY_MAX = 10`).
3. Page reads `?q=` via `URLSearchParams` on mount.
4. Page uses `window.history.replaceState` (not `pushState`).
5. "Recent queries" section header rendered.
6. Clear-history control present.
7. Phase 42 marker in footer.
8. Phase 39 feature test still passes (auth contract guard).

---

## 7. Out of scope for Phase 42

- Server-side history (per-user, queryable). Slice 4 is
  intentionally localStorage-only — single-shot search doesn't
  need cross-device sync, and Chat's thread history already
  handles the multi-turn case server-side.
- Persisting full result snapshots for offline replay (could be a
  `v2` of the storage key later).
- History panel pinning, search-within-history, or grouping by date.
- Cancel-in-flight button (still Chat's pattern, omitted here).

---

## 8. Files of record (slice 4)

```
resources/js/Pages/SearchQuery.tsx        — 286 → ~400 lines (+~115)
scripts/phase42_step1_verify.sh           — new (verifier)
docs/phase42_handoff.md                   — this file
scripts/phase42_master_sweep.sh           — sweep (41 → 42)
```

No backend changes. No new dependencies. No new components.

---

## 9. R-P11-B progress

| Slice | Phase | Status |
|-------|------:|--------|
| 1 — skeleton + route + test | 39 | ✅ |
| 2 — SSE submission | 40 | ✅ |
| 3 — citation rendering | 41 | ✅ |
| 4 — history + deep-link | 42 | ✅ this phase |
| 5 — top-nav + cleanup | 43 | pending — last slice |

End of Phase 42 handoff.
