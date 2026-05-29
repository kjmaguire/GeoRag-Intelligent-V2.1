# Phase 55 Handoff — Master-plan §3 Step 7a (orchestrator)

**Document version:** 1.0
**Status:** Doc-phase 55 complete. Doc-phase 56 inheriting.
**Predecessors:** `docs/phase54_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

Step 7 is the integration step in master-plan §3. To keep doc-phase
tick sizes bounded, Step 7 is being split across three ticks:

- **doc-phase 55 (THIS TICK)**: orchestrator that chains
  preflight → profile → parsers → routing → summary
- **doc-phase 56 (NEXT)**: persistence layer that writes orchestrator
  output to the 8 silver tables
- **doc-phase 57 (AFTER)**: Hatchet `ingest_pdf` step rewrite to
  invoke orchestrator + persistence

Shadow comparison (dual-run RAGFlow + §04p) is a fourth, separate
tick if scope permits (likely doc-phase 58).

---

## 1. What doc-phase 55 delivered

`app.ocr._orchestrator.orchestrate(pdf_path)` — an async function
that runs the entire §04p decision flow against a single PDF and
returns a structured result the persistence layer can write.

### Orchestrator return shape

```python
{
    "preflight": dict,                # preflight result
    "profile": dict | None,           # None if preflight failed
    "parses": {
        "native": dict | None,
        "scanned": dict | None,        # may include retry merged
        "mixed": dict | None,
        "table_heavy": dict | None,
    },
    "route_decisions": list[dict],    # per page, post-retry
    "document_summary": dict,         # summarize_document() output
    "retry_log": list[dict],          # diagnostic per-retry attempts
}
```

### Flow

```
1. preflight                          → preflight_result
2. if not valid → bail with reject doc-level recommendation
3. profile                            → profile_result
4. dispatch parsers by document_profile:
     native      → parse_native
     scanned     → parse_scanned
     mixed       → parse_mixed + parse_scanned(pages_needing_ocr)
     table_heavy → parse_table_heavy
     map_heavy   → no parser (every page routes to review)
5. for each page: route_page (with preflight + retry_count=0)
6. for each re_ocr decision: re-run parse_scanned with escalated
   settings, merge into the parse result, re-route, repeat up to
   MAX_OCR_RETRIES
7. summarize_document over all per-page routes → recommended_action
```

### Internal module convention

The orchestrator lives at `app.ocr._orchestrator` (leading underscore
in module name). It is NOT re-exported from `app/ocr/__init__.py`.
Only the Hatchet `ingest_pdf` workflow (when rewritten in doc-phase
57) + tests should import it. Keeps the public OCR surface focused
on the individual parsers + quality_graph.

---

## 2. Files of record

### New
- `src/fastapi/app/ocr/_orchestrator.py`
- `src/fastapi/tests/test_ocr_orchestrator.py` (9 tests, 1.29 sec)
- `scripts/phase3_master_plan_step7a_verify.sh`

### Modified
- None — orchestrator is pure additive

---

## 3. Verifier status

```
[check1] PASS — _orchestrator.orchestrate is importable + async
[check2] PASS — 9/9 orchestrator behaviour tests green
[check3] PASS — Step 1 verifier still green
[check4] PASS — Step 2 verifier still green
[check5] PASS — Step 3 verifier still green
[check6] PASS — Step 4 verifier still green
[check7] PASS — Step 5 verifier still green
[check8] PASS — Step 6 verifier still green

=== Phase 3 master-plan Step 7a verifier summary ===
  8/8 checks passed
```

Pytest scoreboard (full OCR suite):
- **61 OCR tests passing** (52 from prior ticks + 9 new orchestrator tests)
- All 8 modules graduated; all verifiers green

---

## 4. Decisions made in this phase

### 4.1 Step 7 split across 3 doc-phase ticks (55/56/57)

The kickoff Step 7 deliverables (Hatchet rewrite + persistence +
shadow comparison) bundle three architecturally different concerns
into one step. Splitting per the autonomous-loop cadence
("1-3 small steps per tick") keeps each tick reviewable.

Step 7a (this tick): orchestrator — pure-ish, no DB, no Hatchet
Step 7b (next): persistence — DB writes, real silver rows
Step 7c (after): Hatchet step rewrite — wires the chain together
Step 7d (optional): shadow comparison — dual-run RAGFlow + §04p

### 4.2 Retry loop lives in the orchestrator, not the Hatchet step

route_page decides "re_ocr with these settings"; the orchestrator
calls parse_scanned again, merges the retry result back into the
current parse_result, and re-routes. The Hatchet step (doc-phase 57)
will be a thin wrapper that calls `orchestrate(pdf_path)` once and
then hands the result to the persistence layer.

This keeps the retry semantics in one place — easier to test, easier
to reason about. The Hatchet step doesn't need to know about retry
state.

### 4.3 Mixed documents may run TWO parsers

The mixed-profile dispatch runs parse_mixed (Docling layout) THEN
parse_scanned on pages_needing_ocr. Both parse results live side-by-side
in `result["parses"]`. The route_page call per page picks the
parser-output relevant to that page's profile via the
`_parse_result_for_page` helper.

This is the only "two parsers per document" case. Native, scanned,
table_heavy, map_heavy each run exactly one parser (or none).

### 4.4 `_merge_retry_into_parse` updates only the retried page

When re_ocr fires for page N, parse_scanned is re-invoked with
`pages=[N]`. The retry's output has length-1 arrays — index 0
corresponds to page N. The merge helper splices retry[0] into
position N of the base arrays, preserving everything else. Passages
on page N get replaced; passages on other pages preserved.

This is the cleanest semantic — retry replaces page N's data, all
other pages keep their original parse output.

### 4.5 Preflight-fail produces a single doc-level reject decision

When preflight is invalid (encrypted, corrupted, not-a-PDF),
the orchestrator does NOT call profile or any parser. The
`document_summary` is built from a synthetic single-page reject
decision so the persistence layer can still write a
`silver.document_ingestion_quality.recommended_action = "reject"`
row with a reason code. No `silver.ingest_*` rows are written
because no parsing happened.

---

## 5. Findings carried over to doc-phase 56+

### 5.1 Persistence layer schema mapping

doc-phase 56 (`_persist.py`) needs to map orchestrator output → 8
silver tables. Mapping plan:

| Orchestrator field | Silver table | Notes |
|---|---|---|
| `preflight` | `silver.parser_run_artifacts` (1 row, parser_used="preflight") | |
| `profile.per_page_profiles` | (no direct write; informs per-page routing) | |
| `profile` (whole) | `silver.parser_run_artifacts` (1 row, parser_used="profiler") | |
| `parses[X].passages` | `silver.ingest_extractions` (native, mixed text regions) OR `silver.ingest_ocr_results` (scanned) | per-region rows |
| `parses["mixed"].layouts` | `silver.ingest_layouts` | per-region with layout_label |
| `parses[X].tables` | `silver.table_extraction_quality` + `silver.tables` (existing) | per-table rows |
| `route_decisions` | per-page row in `silver.ocr_page_quality` with parser_used + confidences + needs_review | |
| `route_decisions[?].route == silver_review` | `silver.low_confidence_page_reviews` (1 row per review-routed page) | |
| `document_summary` | `silver.document_ingestion_quality` (1 row per document) | |

### 5.2 Bbox coord_origin still needs handling in persistence

parse_native + parse_mixed return BOTTOMLEFT (PDF coords); parse_scanned
returns TOPLEFT image coords (post-render-scale). Doc-phase 56's
persist layer needs to translate one to the other before writing to
`silver.ingest_extractions.bbox`. Simplest: store both with a
`coord_origin` annotation in the JSONB payload column; let
downstream consumers decide.

### 5.3 Workspace_id GUC pattern

Per the Phase 0 RLS design, every silver write needs
`SET LOCAL app.workspace_id = '<uuid>'` on the connection before
the INSERT. doc-phase 56 must include this in the persist helpers.

### 5.4 `silver.document_passages` is existing — separate concern

The orchestrator doesn't include silver.document_passages writes
(that's the existing retrieval-pipeline table). doc-phase 56 should
decide whether to also write to that table (matching the v1.49
output) or leave it alone (the new §04p path uses
silver.ingest_extractions for per-region data, and downstream
retrieval can read from there).

Likely answer: write to BOTH for doc-phase 56-57. silver.document_passages
keeps existing retrieval working; silver.ingest_extractions is the
new authoritative per-region store. RAGFlow retirement in doc-phase
58 may eventually flip retrieval to read from ingest_extractions
directly.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From prior handoffs — all still open:
- Profile classifier thresholds need 50-PDF corpus tuning (Step 9)
- Table review confidence thresholds need 50-PDF corpus tuning (Step 9)
- Migration apply path workaround
- Windows ↔ WSL dual-tree sync
- WSL2 exposes 6/32 CPUs
- PaddleOCR cache → /tmp by default
- Docling deprecation warning on table image extraction (benign)
- Retry settings escalation logic is opinionated

From doc-phase 48:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch

---

## 7. What doc-phase 56 will do

**Master-plan §3 Step 7 part B — `app/ocr/_persist.py` persistence layer.**

Build the persist helpers that take orchestrator output + a workspace
ID + a report ID and write rows to all 8 silver tables (plus
parser_run_artifacts rows for each parser invocation).

Deliverables:
- `app.ocr._persist.persist_orchestrator_result(conn, workspace_id, report_id, result)`
  — one transactional write that lands rows in all relevant tables
- `app.ocr._persist.transactional_workspace_session(pool, workspace_id)`
  — context manager that sets the RLS GUC + opens a transaction
- Bbox coord_origin annotation in the JSONB payload column
- Integration test that:
  - Creates a test workspace + project + report (or uses existing
    fixtures)
  - Runs the orchestrator on PLS-2024
  - Calls persist with the result
  - Queries each silver table and asserts row counts + key columns
- New verifier `scripts/phase3_master_plan_step7b_verify.sh`

Test wall-time should be modest (~5-10 sec; DB writes are fast,
the orchestrator runs in ~1 sec on the native fixture).

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
| 7a. Orchestrator | ✅ DONE | 55 |
| 7b. Persistence layer | next | 56 |
| 7c. Hatchet ingest_pdf rewrite | pending | 57 |
| 7d. Shadow comparison (optional) | pending | 58 |
| 8. Silver Review UI extension | pending | 58-59 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 59-60 |
| 10. RAGFlow retirement + cleanup | pending | 60-61 |

**6.5 of 10 steps complete.** Step 7 split into 4 sub-ticks; 7a
landed cleanly.

---

End of doc-phase 55 handoff. Orchestrator chains the §04p stack
end-to-end on PDFs. Persistence next.
