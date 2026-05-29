# Phase 41 Handoff — R-P11-B slice 3 (citation rendering)

**Document version:** 1.0
**Status:** Phase 41 slice 3 complete. Search/Query page now opens the same EvidenceInspector overlay that Chat.tsx uses, on citation click.
**Predecessors:** `docs/phase40_handoff.md`.

---

## 1. What Phase 41 slice 3 delivered

Slice 3 of R-P11-B replaces the slice-2 raw-JSON citation stub with
the proper composition pattern: typed `Citation[]` on the result
state, a click-to-open list, and the existing
`<EvidenceInspector>` shadcn Sheet for the detail surface. No new
component was created — slice 3 is purely composition of existing
chat-side primitives, which keeps both surfaces in lockstep when the
inspector evolves.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `resources/js/Pages/SearchQuery.tsx` — added `EvidenceInspector` + `Citation` imports, typed the `SearchResult.citations` field, added `InspectorState`, replaced the `<details>` JSON stub with a click-to-open citation list, mounted the inspector overlay at page bottom. | `scripts/phase41_step1_verify.sh` (8/8) |
| 2 | This handoff + master sweep | — |

---

## 2. Citation list render

```
┌──────────────────────────────────────────────────────────┐
│ 3 CITATIONS                                              │
│ ┌──────────────────────────────────────────────────────┐ │
│ │ 📄 Triple R Technical Report 2023               NI43 │ │
│ │    Section 14 — Mineral Resource · p.142 · 87%       │ │
│ ├──────────────────────────────────────────────────────┤ │
│ │ 📊 PLS-22-08 assay batch                        DATA │ │
│ │    — · 79%                                           │ │
│ ├──────────────────────────────────────────────────────┤ │
│ │ 🗺️ NTGS Surface Geology 1:250k                  PGEO │ │
│ │    Solid geology layer · 64%                         │ │
│ └──────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

Each row is a `<button>` that pushes `{open: true, evidenceId,
legacyCitation}` into the inspector state. The same evidence sheet
that Chat.tsx opens then takes over rendering — including the
`/v1/evidence/{evidenceId}` fetch + legacy fallback when only the
SSE citation payload is available.

The citation-kind icon map covers all four current `citation_type`
enum values (`NI43`, `PUB`, `DATA`, `PGEO`) — kept inline as a
4-key record rather than importing icon logic from `CitationMarker`
to avoid pulling shadcn deps the search page doesn't need.

---

## 3. Why no `<ChatMessage>` reuse

The kickoff doc mentioned a `<ChatMessage>` reuse as a slice 3
option, but ChatMessage is conversation-shaped: it expects
`role`/`status`/`phases`/`thread_id` fields and renders inline
citation markers parsed out of the answer text via a regex
(`CITATION_RE`). Search renders a single answer with a separate
citation list, not inline markers, so importing ChatMessage would
require wrapping the search result in a synthetic chat-message
object with placeholder fields. The composition cost outweighs
the reuse benefit — the search-side citation list is ~35 LOC and
keeps the page focused on its single-shot UX.

The EvidenceInspector reuse, by contrast, is clean: it takes only
`open` / `onOpenChange` / `evidenceId` / `legacyCitation` and is
already designed for both chat-citation clicks and refusal-panel
nearest-candidate clicks. The search-citation click is a third
caller, no adapter needed.

---

## 4. Verifier shape

`scripts/phase41_step1_verify.sh` runs 8 checks:

1. EvidenceInspector import present.
2. `Citation` type imported from `@/types`.
3. `<EvidenceInspector />` element rendered.
4. Inspector state covers `open` / `evidenceId` / `legacyCitation`.
5. Citation kind icon map covers all 4 `citation_type` values.
6. Raw-JSON citation stub from slice 2 removed (negative check on
   `JSON.stringify(result.citations` and the slice-2 placeholder
   phrase — the unrelated `JSON.stringify` on the create POST body
   is intentionally permitted).
7. Phase 41 marker present in footer.
8. Phase 39 feature tests still pass — auth contract regression
   guard.

---

## 5. Out of scope for Phase 41

- History panel + URL deep-link (slice 4).
- Top-nav entry + placeholder cleanup (slice 5).
- `<CitationPGEODetail>` direct rendering — `EvidenceInspector`
  already delegates to the LegacyCitationRenderer for PGEO
  citations, so explicit composition isn't needed at this layer.
- Inline citation markers in the answer text — search is
  single-shot, not narrative-with-inline-cites; the separate list
  is the documented UX.

---

## 6. Files of record (slice 3)

```
resources/js/Pages/SearchQuery.tsx        — 217 → 286 lines (+69)
scripts/phase41_step1_verify.sh           — new (verifier)
docs/phase41_handoff.md                   — this file
scripts/phase41_master_sweep.sh           — sweep (40 → 41)
```

No backend changes. No new components. No new dependencies. Phase
39 feature test is untouched and still passes (auth contract
unchanged).

---

## 7. R-P11-B progress

| Slice | Phase | Status |
|-------|------:|--------|
| 1 — skeleton + route + test | 39 | ✅ |
| 2 — SSE submission | 40 | ✅ |
| 3 — citation rendering | 41 | ✅ this phase |
| 4 — history + deep-link | 42 | pending |
| 5 — top-nav + cleanup | 43 | pending |

End of Phase 41 handoff.
