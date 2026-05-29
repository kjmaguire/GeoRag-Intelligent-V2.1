# §6b — Spatial / Visualisation Chat-Card Audit

**Date:** 2026-05-29
**Status:** Audit-only. No code changes in this pass.
**Verdict:** **Substantially shipped.** Eight card types live + backend builder + per-card frontend components + lazy-loading + per-card dismissal. Polish gaps identified below, but the surface is *not* the from-scratch frontend work the original Phase 5 sizing assumed.

This document corrects a sizing error from my Phase 1→6 dependency-chain analysis (2026-05-29). I had §6b estimated at 10-15h of focused frontend work. That estimate was off — most of the surface is built. This audit captures what's solid, what's polish-only, and what's genuinely missing.

## Surface overview

```
agentic graph                  Laravel SSE bridge        React Chat.tsx
─────────────                  ──────────────────        ──────────────
assemble_node                  /v1/query                 GeoRAGResponse
  │                              │                         │
  ├─ _build_chat_card_payloads ──┘                         │
  │  returns (map, viz)                                    │
  │                                                        │
  └─ response.map_payload                                  │
     response.viz_payload                                  │
       │                                                   │
       └─ Reverb broadcast ───────────────────────────────► InlineViz
                                                            │
                                                            ├─ MapView          (geojson)
                                                            ├─ StripLogViewer   (chart_type='downhole_strip')
                                                            ├─ GeoPlot          (chart_type='assay_histogram' | 'cross_section')
                                                            ├─ KnowledgeGraph   (chart_type='graph_viz')
                                                            ├─ DrillTrace3D     (chart_type='drill_trace_3d')
                                                            ├─ TimelineCard     (chart_type='technique_timeline')
                                                            ├─ CoverageTableCard(chart_type='coverage_table')
                                                            └─ StereonetCard    (chart_type='stereonet')
```

## What's shipped (✅)

### Backend — `_build_chat_card_payloads`

Location: `src/fastapi/app/agent/agentic_retrieval/nodes.py:898`

Produces `(map_payload, viz_payload)` from the agentic graph's `tool_results`. Dispatches per-intent + per-tool-result-type:

- `DrillTrace3DResult` → `chart_type='drill_trace_3d'` (precedence over intent-specific cards)
- `project_summary` intent → `chart_type='technique_timeline'` + breakdown_table meta
- `coverage_gap` intent → `chart_type='coverage_table'`
- `StereonetResult` → `chart_type='stereonet'` (image_base64 in meta)
- Spatial collars → MapPayload with GeoJSON + bbox

Wired into the response at `assemble_node` line 875-884.

### Frontend dispatcher — `InlineViz`

Location: `resources/js/Components/InlineViz.tsx` (170 LOC).

- 8 distinct render branches, all lazy-loaded via `React.lazy(...)` so the bundle only pulls Plotly / MapLibre / Three.js on actual render
- Per-card dismissal via `mapHidden` / `vizHidden` state
- Renders nothing when both payloads are null (no UI noise on text-only queries)
- Loading panels with spinner per Suspense boundary

### Frontend cards (8 components)

| Component | Type | Test |
|---|---|---|
| `MapView.tsx` | spatial | ✅ 4 tests (auth, layerActivation, layerToggle, tileUrl, uncertaintyRings) |
| `StripLogViewer.tsx` | downhole | ✅ 1 test |
| `GeoPlot.tsx` | histogram / cross-section | ⚠️ no dedicated test |
| `KnowledgeGraph.tsx` | graph_viz | ⚠️ no dedicated test |
| `DrillTrace3D.tsx` | 3d | ✅ 1 test |
| `TimelineCard.tsx` | technique_timeline | ✅ 1 test |
| `CoverageTableCard.tsx` | coverage_table | ✅ 1 test |
| `StereonetCard.tsx` | stereonet | ✅ 1 test |

### Chat.tsx integration

Location: `resources/js/Pages/Foundry/Chat.tsx:705-712`.

Renders `<InlineViz>` when message has `mapPayload || vizPayload`. Companions:

- `<EvidencePacketBadge>` at line 718 — per-kind counts + budget pressure
- `<ResolutionPreviewChip>` at line 723 — multi-turn rewrite preview

## Polish backlog (⚠️)

Ranked by user-visible impact:

### P1 — TypeScript discipline

`resources/js/Components/InlineViz.tsx:1` opens with `// @ts-nocheck`, disabling type checking for the whole file. The cast-heavy implementation (`meta as Record<string, unknown>`, `... as any[]`, etc.) suggests the discriminated-union for `VizPayload.chart_type` was never authored.

**Fix path:**
1. Define a proper `VizPayload` discriminated union in `resources/js/types.ts` — one variant per `chart_type` value, each with the meta shape required by its card.
2. Remove `@ts-nocheck`, replace `as any[]` casts with a type-narrowing switch.
3. `tsc --noEmit` clean.

**Sizing:** ~3-4h. Low risk — pure refactor with no runtime change.

### P2 — Backend `_build_chat_card_payloads` tests

`grep -l _build_chat_card_payloads src/fastapi/tests/*.py` returns zero results. The function dispatches across 5+ intents / result types with edge cases (DrillTrace3D precedence, coverage_gap with MapPayload deferred to PR-2, etc.) — every dispatch path needs a fixture.

**Fix path:**
1. New `tests/test_chat_card_payloads.py` with one test per intent / tool-result type
2. Mock `tool_results` lists with deterministic fixtures
3. Assert `(map_payload, viz_payload)` shape matches the frontend's expected meta keys

**Sizing:** ~3-4h. 8-12 unit tests.

### P3 — Frontend dispatcher tests

`InlineViz` has 0 dedicated tests. The dispatch logic (which `chart_type` triggers which card, what `mapHidden` does, what happens when payloads are partial) is non-trivial.

**Fix path:**
1. New `resources/js/Components/__tests__/InlineViz.test.tsx`
2. Render with each `chart_type` fixture; assert correct card mounts
3. Test partial payloads (map only / viz only / neither)
4. Test per-card dismiss behaviour

**Sizing:** ~2-3h. ~10 vitest tests.

### P4 — Missing `coverage_gap` MapPayload

`_build_chat_card_payloads` docstring notes: *"The MapPayload is currently None — building a real GeoJSON requires re-querying collar geometries which the coverage tool doesn't return. PR-1 ships the table only; the spatial-holes map is a PR-2 follow-up."*

A `coverage_gap` query gets a table but no spatial visualisation of where the gaps are. The PR-2 follow-up is still pending.

**Fix path:**
1. Extend the coverage_gap tool result to include collar geometries
2. Build GeoJSON from result.gap_collars
3. Frontend already has MapView; no UI change needed

**Sizing:** ~3-4h.

### P5 — `chart_type` enum centralisation

The discriminator strings (`'downhole_strip'`, `'assay_histogram'`, `'cross_section'`, `'graph_viz'`, `'drill_trace_3d'`, `'technique_timeline'`, `'coverage_table'`, `'stereonet'`) are inlined as string literals in `InlineViz.tsx`. The backend `_build_chat_card_payloads` has its own copies. A typo on either side silently breaks the card rendering.

**Fix path:**
1. Centralise in `src/fastapi/app/models/rag.py` as a Literal type
2. Export to TypeScript via Pydantic schema generation (or a hand-maintained `types.ts` enum if that's lighter)
3. Add a regression test asserting both sides agree

**Sizing:** ~1-2h. Bundles naturally with P1.

### P6 — Coverage / DrillTrace3D / Stereonet observability

Per the `shadow_telemetry_sentry_tags.md` spec, the `evidence.has_spatial` Sentry tag fires on spatial queries. But there's no tag that says *which card type rendered*, so dashboards can't measure (say) "% of queries that produced a 3D card" or "% of stereonet queries that succeeded".

**Fix path:**
1. Extend `stamp_evidence_tags` in `sentry_tags.py` to emit a `card_type` tag from the produced viz_payload
2. Test fixture per card type

**Sizing:** ~1h. Bundles naturally with §I tag-setter work.

## Genuinely missing (🔴) — not "polish"

None identified. The §6b surface as specified is shipped.

## Sentry tags spec reference

Per `docs/architecture/shadow_telemetry_sentry_tags.md`:

> `evidence.has_spatial`: bool — "Triggers the §6b MapLibre card. Tagged for quick 'show me spatial queries' filtering."

This tag IS being stamped (per the morning's Sentry re-enable wire). The §6b card itself rendering is downstream — visible to the user, not directly tagged.

## Net assessment

| Item from original Phase 5 sizing | Actual state | Original sizing | Real sizing |
|---|---|---|---|
| §6a — data page UI | UI is small; blocked on writers | 6-10h | 6-10h *after* writers ship (~1-2 days) |
| §6b — MapLibre full integration | ~85% shipped; polish needed | 10-15h | 8-12h polish (P1-P6 above) |
| §6c — reranker deployment | Gated on §5e | 2-3h | Unchanged |

The Phase 5 estimate as "16-25h of frontend work" was wrong. The real frontend opportunity is **8-12h of polish on §6b** (P1-P6), then §6a remains blocked behind 1-2 days of Dagster writer work.

## Recommendation for next focused session

If you want frontend polish: pick P1 (TypeScript discipline) + P2 (backend tests) + P3 (frontend dispatcher tests) — these are the highest-leverage hygiene wins. Roughly an 8-11h focused session that ships:
- A typed `VizPayload` discriminated union (TS clean, no casts)
- 10+ backend unit tests pinning the dispatch contract
- 10+ frontend tests pinning the rendering contract
- Removed `@ts-nocheck` directive

If you want user-visible feature work: P4 (coverage_gap MapPayload) is ~3-4h and meaningfully improves the coverage_gap response surface.

If you want to unblock §6a UI: stop reading this audit and start writing the `silver.data_quality_flags` writers (5 Dagster rule families per `data_quality_flags_design.md`). The badge UI itself is ~2h on top of that.
