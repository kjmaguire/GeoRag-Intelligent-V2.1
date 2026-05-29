# Phase 40 Handoff — R-P11-B slice 2 (SSE submission)

**Document version:** 1.0
**Status:** Phase 40 slice 2 complete. Search/Query page now submits live queries through the same handshake Chat.tsx uses.
**Predecessors:** `docs/phase39_handoff.md`.

---

## 1. What Phase 40 slice 2 delivered

Slice 2 of R-P11-B: the SSE wiring. The page now performs the full
three-step handshake against the existing Laravel + Reverb stack —
no backend changes required.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `resources/js/Pages/SearchQuery.tsx` — grew from 69 → 217 lines. Added SSE state machine (phase / error / result), Echo channel subscription + cleanup, completed/failed/status event handling, and a raw-JSON citation block as a slice-3 placeholder. | `scripts/phase40_step1_verify.sh` (8/8) |
| 2 | This handoff + master sweep | — |

The auth contract is unchanged (route still inline `Inertia::render`),
so the Phase 39 feature test continues to pass without modification.

---

## 2. Handshake (mirrors Chat.tsx:482-797)

```
                ┌─────────────┐   POST /api/v1/queries        ┌──────────────┐
   User submits │             │ ──────────────────────────▶   │  Laravel API │
   "Ask"        │ SearchQuery │                                │              │
                │  (browser)  │ ◀──── { query_id, channel } ──┤              │
                └──────┬──────┘                                └───────┬──────┘
                       │                                               │
                       │ window.Echo.channel(channel)                  │
                       │ .listen('.QueryStreamEvent', …)               │
                       │                                               │
                       │ POST /api/v1/queries/{id}/start              │
                       ├──────────────────────────────────────────────▶│
                       │                                               │
                       │   ◀── status events while pipeline runs ──    │
                       │   ◀── final 'completed' event                 │
                       ▼
                  setResult({ answer, citations, confidence })
```

The page leaves the Echo channel and stops listening as soon as a
terminal event (`completed`, `failed`, or `error`) arrives, plus on
any thrown exception during the create/start POSTs. Submitting a new
query also tears down the previous subscription before opening a new
one, so the page never holds more than one live channel.

---

## 3. State machine

| State | UI |
|-------|----|
| Idle | Empty grey "Results will appear here once you submit a query." card |
| Busy (`phase` non-null) | Italic `aria-live="polite"` status line; input + Ask disabled |
| Error | Red error box with the exception message |
| Result | White card with answer, confidence pill, expandable citations block |

`phase` is a single string (the latest status event message or
`'Submitting query…'` / `'Waiting for response…'`). The full
multi-phase progress trail that Chat.tsx renders is intentionally
omitted — single-shot search doesn't need a checklist of stages.

---

## 4. Why no project_id

Chat.tsx sends `project_id` from its workspace context. Search at
slice 2 doesn't have a project picker (slice 4 of R-P11-B will add
one if needed for history scoping). The endpoint accepts requests
without `project_id`; the server side handles the unscoped case the
same way it does for a fresh chat session with no project selected.

---

## 5. Citation rendering — deferred to slice 3

Citations come back as `event.citations` on the `completed` event.
Slice 2 renders them as an expandable `<details>` block with
JSON.stringify so the data is visible during dev. Slice 3 will
swap that for the `<CitationPGEODetail>` + `<EvidenceInspector>`
components that Chat.tsx already uses (no new components needed —
just composition).

---

## 6. Verifier shape

`scripts/phase40_step1_verify.sh` runs 8 checks:

1. Page grew to ≥ 120 lines (slice 1 was 69; slice 2 lands at 217).
2. Page POSTs to `/api/v1/queries`.
3. Page subscribes to AND leaves the Echo channel (paired calls).
4. Page POSTs to `/api/v1/queries/{id}/start`.
5. Page listens for `QueryStreamEvent`.
6. Page handles all three terminal/intermediate event types:
   `status`, `completed`, `failed`.
7. Page contains the "Phase 40" marker for the cumulative slice trail.
8. Phase 39 feature tests still pass — auth contract regression
   guard (no DB-side change should break the guest/auth redirect
   behaviour).

---

## 7. Out of scope for Phase 40

- Citation component reuse (slice 3).
- Per-query persistence + history panel (slice 4).
- Top-nav entry + placeholder cleanup (slice 5).
- Workspace / project picker.
- Cancel-in-flight button (Chat.tsx doesn't have one either; the
  5-min safety timeout from Chat is also omitted here — Phase 41
  may revisit).

---

## 8. Files of record (slice 2)

```
resources/js/Pages/SearchQuery.tsx        — 69 → 217 lines (+148)
scripts/phase40_step1_verify.sh           — new (verifier)
docs/phase40_handoff.md                   — this file
scripts/phase40_master_sweep.sh           — sweep (39 → 40)
```

No backend changes. No new dependencies. No PHP changes (Pint
no-op). The Phase 39 feature test is untouched and still passes.

---

## 9. R-P11-B progress

| Slice | Phase | Status |
|-------|------:|--------|
| 1 — skeleton + route + test | 39 | ✅ |
| 2 — SSE submission | 40 | ✅ this phase |
| 3 — citation rendering | 41 | pending |
| 4 — history + deep-link | 42 | pending |
| 5 — top-nav + cleanup | 43 | pending |

End of Phase 40 handoff.
