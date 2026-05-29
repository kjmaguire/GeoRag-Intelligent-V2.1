# Phase 54 Handoff — Master-plan §3 Step 6 (OCR Quality Graph)

**Document version:** 1.0
**Status:** Doc-phase 54 complete. Doc-phase 55 inheriting.
**Predecessors:** `docs/phase53_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

The last `app.ocr.*` skeleton graduates. **All 8 OCR modules now
implemented.** Step 7 (Hatchet ingest_pdf cutover + persistence) is
the integration step — wire the parsers + quality graph to the silver
tables that landed in doc-phase 50.

---

## 1. What doc-phase 54 delivered

`app.ocr.quality_graph` with two public functions:

| Function | What it does |
|---|---|
| `route_page(parse_result, page, profile, preflight=None, retry_count=0, thresholds=None)` | Per-page routing decision: accept / re_ocr / silver_review / reject |
| `summarize_document(route_decisions, page_profiles=None)` | Per-document `recommended_action` from a list of per-page routes |

### Routing decision tree (per page)

```
preflight invalid?  ──── yes ───→ reject (reason = encrypted_section /
                                         page_blank_or_corrupted / other)
        no
        │
profile == map_heavy? ── yes ───→ silver_review (reason =
                                                map_heavy_v1_deferral)
        no
        │
profile ∈ {native, table_heavy}?
        yes:
          - 0 passages → silver_review (page_blank_or_corrupted)
          - low table structure conf → silver_review (table_confidence_below_threshold)
          - else → accept

profile ∈ {scanned, mixed}:
          - 0 text lines → silver_review (page_blank_or_corrupted)
          - ocr_conf ≥ 0.85 + layout OK → accept
          - ocr_conf < 0.50 → silver_review (ocr_confidence_below_threshold)
          - else (marginal) → re_ocr if retries < 2,
                              else silver_review (retry_max_exceeded)
```

### Module-level threshold constants (Step 9 tunable)

```python
ACCEPT_OCR_CONFIDENCE = 0.85
REVIEW_OCR_CONFIDENCE = 0.50
ACCEPT_LAYOUT_CONFIDENCE = 0.70
REVIEW_LAYOUT_CONFIDENCE = 0.40
MAX_OCR_RETRIES = 2
```

### Retry settings escalation (RETRY_SETTINGS_BY_ATTEMPT)

```python
[
    {"render_scale": 3.0, "use_angle_cls": True, "lang": "en", ...},  # attempt 1
    {"render_scale": 4.0, "use_angle_cls": True, "lang": "en", ...},  # attempt 2
]
```

Step 7 orchestrator reads the `retry_settings` from a `re_ocr` decision
and passes them as `settings=` to the next `parse_scanned` call.

---

## 2. Files of record

### New
- `src/fastapi/tests/test_ocr_quality_graph.py` (19 tests, sub-second runtime)
- `scripts/phase3_master_plan_step6_verify.sh`

### Modified (skeleton → implementation)
- `src/fastapi/app/ocr/quality_graph.py`
- `src/fastapi/app/ocr/__init__.py` — added `summarize_document` to `__all__`
- `src/fastapi/tests/test_ocr_module_imports.py` — `SKELETON_MODULES` now empty

---

## 3. Verifier status

```
[check1] PASS — 19/19 quality_graph tests green
[check2] PASS — SKELETON_MODULES is empty (all 8 modules graduated)
[check3] PASS — route_page + summarize_document exported from app.ocr
[check4] PASS — Step 1 verifier still green
[check5] PASS — Step 2 verifier still green
[check6] PASS — Step 3 verifier still green
[check7] PASS — Step 4 verifier still green
[check8] PASS — Step 5 verifier still green

=== Phase 3 master-plan Step 6 verifier summary ===
  8/8 checks passed
```

Pytest scoreboard for full OCR suite:
- 19 quality_graph tests (sub-second)
- 9 mixed + table-heavy tests
- 7 scanned tests
- 8 native tests
- 9 module-import tests pass; **all 8 skeleton-NotImplementedError tests now SKIP** (correct — every module has implementation)
- **Total: 52 OCR tests passing**

---

## 4. Decisions made in this phase

### 4.1 Pure function, not LangGraph state machine

Master plan §9.7 names this "LangGraph OCR Quality Graph". The actual
per-page decision is a one-shot classification — LangGraph state
machine adds framework complexity without adding capability. The
retry LOOP lives at the Hatchet orchestrator (Step 7), not inside
this module.

**If a future tick demonstrates real value from LangGraph wrapping**
(visualization, replay, branching into other graphs like the Answer
Graph), this module can be wrapped without changing its public API.
The state diagram is documented in the docstring; transitioning to
a LangGraph implementation is a refactor, not a redesign.

### 4.2 `route_page` is fully synchronous logic wrapped async

No I/O, no model inference. The `async def` + `asyncio.to_thread`
pattern is preserved for API consistency with the other `app.ocr.*`
functions, but the underlying work is microseconds of dict access
and branching.

### 4.3 Per-page retry vs document-level retry separation

`route_page` does NOT execute retries — it returns `route="re_ocr"`
with the settings to use, and the orchestrator (Step 7) calls
`parse_scanned` again with those settings, then re-calls `route_page`
with `retry_count+1`. This keeps the routing logic stateless and
testable.

### 4.4 `summarize_document` maps to silver enum

The output's `recommended_action` field uses values that match the
`silver.document_ingestion_quality.recommended_action` CHECK
constraint from doc-phase 50 (`accept`, `accept_with_review`,
`review_all_pages`, `reject`). Step 7 writes this directly to the
column without translation.

### 4.5 `_extract_scores` reads whatever-parser-produced

The score-extraction helper reads `per_page_ocr_confidence`,
`per_page_text_line_counts`, `per_page_text_region_counts`,
`per_page_passage_counts`, `per_page_layout_confidence`,
`per_page_table_counts`, and `tables[].structure_confidence`
defensively — whichever parser produced the result, the helper finds
the right key or defaults. Means `route_page` works against any
of the four parsers' outputs without parser-specific branches.

### 4.6 Threshold override via per-call `thresholds=` kwarg

Production deployment + Step 9 corpus tuning can override any
threshold without code changes. Useful for A/B-testing different
sensitivity levels per workspace or per source corpus.

---

## 5. Findings carried over to doc-phase 55+

### 5.1 Step 7 is the integration step — substantial work ahead

Step 7 wires:
- The Hatchet `ingest_pdf.parse` step calls `preflight → profile →
  parse_*` dispatching on profile result
- For each parsed page, `route_page` decides accept/retry/review/reject
- Re-OCR loop: call parse_scanned again with escalated settings; track retry_count
- Persistence layer: write rows to all 8 silver tables (8 §9.6 + 3 §9.3)
- Workspace_id GUC handling (`SET LOCAL app.workspace_id = <ws>`)
- Bbox coord_origin translation (BOTTOMLEFT native/Docling → unified)
- Shadow comparison: dual-run RAGFlow + §04p, write both to
  `silver.parser_run_artifacts` with `parser_used = "ragflow_shadow"`
  vs `"p04p"` (per the kickoff Step 7 strategy that avoids the
  Phase 47/48 dead-code trap)

This is the most complex step in §3. Likely spans 2-3 doc-phase ticks
unless we cut scope (e.g. shadow comparison deferred to a separate tick).

### 5.2 Retry settings escalation logic is opinionated

The two retry settings dicts in `RETRY_SETTINGS_BY_ATTEMPT` are a
starting point: render at 3.0× scale on retry 1, 4.0× on retry 2.
Real escalation should probably include:
- Binarization threshold variation
- Different language hint
- Toggling `use_angle_cls`

Worth tuning in Step 9 when the corpus reveals which retry strategy
actually rescues which failure mode.

### 5.3 `layout_confidence < review_layout` is the ONE bidirectional dependency

The route decision for scanned/mixed pages with high OCR confidence
ALSO checks layout confidence (from parse_mixed's output). This
means parse_mixed's per_page_layout_confidence is consumed by the
quality graph — a real cross-module data flow. Documented in the
parse_mixed module docstring as part of its return schema.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From prior handoffs:
- Profile classifier thresholds need 50-PDF corpus tuning (Step 9)
- Table review confidence thresholds need 50-PDF corpus tuning (Step 9)
- Migration apply path workaround (use psql + manual INSERT migrations)
- Windows ↔ WSL dual-tree sync
- WSL2 exposes 6/32 CPUs
- PaddleOCR cache → /tmp by default
- Bbox coord_origin handling for Step 7
- Docling deprecation warning on table image extraction (benign)

From doc-phase 48:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch

---

## 7. What doc-phase 55 will do

**Master-plan §3 Step 7 — Hatchet `ingest_pdf` cutover + shadow comparison + persistence.**

This is the integration step that turns the §04p stack from "modules
in isolation that pass unit tests" into "a wired ingest pipeline that
populates silver tables on every PDF upload."

Likely scoped across doc-phases 55-56 if needed. Initial doc-phase 55
focus:
1. Rewrite `app/hatchet_workflows/ingest_pdf.py` parse step to invoke
   the §04p chain (preflight → profile → dispatch parse_* → quality
   graph → retry loop → summarize)
2. Add persistence helper module `app/ocr/_persist.py` that wraps
   asyncpg writes with the workspace_id GUC pattern
3. End-to-end integration test that ingests PLS-2024 through the
   Hatchet step and verifies rows land in all 8 silver tables

Shadow comparison (running RAGFlow alongside §04p for diff capture)
may slip to doc-phase 56 — the kickoff Step 7 lists both but
shipping them together would balloon the tick.

---

## 8. Master-plan §3 progress

| Step | Status | Doc-phase tick |
|---|---|---|
| 1. `app/ocr/` scaffolding + smoke-bench | ✅ DONE | 49 |
| 2. §9.3 + §9.6 silver migrations | ✅ DONE | 50 |
| 3. PDF profiler + native parser | ✅ DONE | 51 |
| 4. Scanned parser + render | ✅ DONE | 52 |
| 5. Mixed + table-heavy parsers (Docling) | ✅ DONE | 53 |
| 6. LangGraph OCR Quality Graph | ✅ DONE | 54 |
| 7. Hatchet `ingest_pdf` cutover + shadow + persistence | next | 55 (+ maybe 56) |
| 8. Silver Review UI extension | pending | 56 or 57 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 57 or 58 |
| 10. RAGFlow retirement + cleanup | pending | 58 or 59 |

**6 of 10 steps complete.** All parser + routing logic implemented
and unit-tested. Step 7 is the wiring phase.

---

End of doc-phase 54 handoff. All `app/ocr/*` modules implemented.
Integration begins doc-phase 55.
