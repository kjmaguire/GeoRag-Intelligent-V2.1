# Phase 11 Scoping — RAG pipeline / ingestion pivot options

**Document version:** 1.0
**Status:** Scoping reference (not an implementation kickoff).
**Generated:** Phase 10 Step 4 via an Explore subagent inventory of the
canonical tree under `src/fastapi/app/agent/`, `src/dagster/georag_dagster/parsers/`,
and the test directories.

---

## 1. Why this doc exists

Phases 7-10 were four "tight ops" phases — integration edge, auth,
observability, admin UI maturation. The natural Phase 11 pivot is the
RAG pipeline / golden queries / ingestion quality. Before Phase 11
opens, we needed to know what was already in the tree so we don't
waste a kickoff doc planning to build things that already exist.

The inventory below is the result. It changed the framing
substantially: the RAG / hallucination-defence framework is
**already implemented**. Phase 11 is about validation and extension,
not greenfield construction.

---

## 2. Inventory snapshot

### 2.1 Parsers (silver-layer ingestion)

12 parser modules under `src/dagster/georag_dagster/parsers/`:

| File | Lines | Format |
|------|-------|--------|
| `pdf_report.py` | 1252 | NI 43-101 PDF reports (canonical; OTel-instrumented) |
| `csv_sample.py` | 952 | Assay + sample data |
| `spatial_parser.py` | 772 | Vector GIS (shapefile, GeoJSON) |
| `csv_collar.py` | 491 | Drill collar locations |
| `csv_lithology.py` | 477 | Lithology layers |
| `csv_survey.py` | 468 | Borehole trajectory |
| `docx_parser.py` | 467 | Word documents |
| `raster_parser.py` | 447 | GeoTIFF, raster GIS |
| `xlsx_parser.py` | 402 | Excel |
| `xyz_parser.py` | 376 | Point clouds |
| `las_parser.py` | 258 | LAS well logs |
| `segy_parser.py` | 211 | Seismic reflection |

Plus utilities: `_csv_io.py`, `_dip_convention.py`, `_encoding.py`,
`_hole_id.py`, `_survey_interp.py`.

### 2.2 Agent code (RAG / orchestration)

**30 files** under `src/fastapi/app/agent/`:

- `orchestrator.py` — 5184 lines, the agent state machine
- `tools.py` — 1632 lines, agent tool definitions
- `citation_binding.py` — 376 lines, output→source linking
- `response_assembler.py` — 470 lines, final response formatting
- `viz_builder.py` — 524 lines, visualization generation
- `public_geoscience_tool.py` — 535 lines, public-geoscience API wrapper
- `model_routing.py`, `llm_classifier.py`, `agentic_escalation.py`,
  `drill_targeting.py`

### 2.3 Section 04i hallucination defence (already implemented)

Ten files under `src/fastapi/app/agent/` covering the six-layer
hallucination-prevention framework from `georag-architecture.html`
Section 04i:

- Layer 1-6 implementations
- `completeness` checker
- `validators` module
- `qualitative_detector` module

### 2.4 Tests

- `src/fastapi/tests/` — **49 files**
- `src/dagster/tests/` — **29 files**
- `tests/` (Laravel) — **56 files**
- Golden query suites already exist:
    - `test_golden_queries.py`
    - `test_public_geoscience_golden.py`

`source_chunk_id` (the citation field per CLAUDE.md hard rule 4)
referenced in 17 test files.

### 2.5 Frontend RAG surfaces

- `QueryUsagePanel.tsx` (analytics)
- `QuerySparkline.tsx` (dashboard)
- **No dedicated Search/Query page in `resources/js/Pages/`**

### 2.6 Routes

`routes/api.php`:
- `v1/` prefix on most agent routes
- `queries/{queryId}/start` — agent invocation endpoint
- `portfolio/query-activity` — analytics

---

## 3. Notable gaps

The inventory surfaced three notable gaps:

1. **No `prompts/` subdirectory** under `src/fastapi/app/agent/`.
   The pre-commit hook from Phase 5 Step 3 watches `src/fastapi/app/agent/prompts/.*`
   but the directory itself isn't there yet. Prompt-versioning
   discipline (`_SYSTEM_PROMPT_VERSION` bookkeeping) is referenced
   from a few places (`orchestrator.py`, cache, classifier) but
   the canonical Prompt-as-File pattern is incomplete.

2. **No Search / Query page** in the React frontend. The agent
   orchestrator is exposed via `v1/queries/...` REST endpoints, but
   there's no Inertia page that consumes them. The "ask a question,
   see a cited answer" UX is unrealised.

3. **Frontend-side citation rendering** is also unimplemented.
   `source_chunk_id` flows through 17 backend tests, but no React
   component renders citations against a chunk-store lookup.

---

## 4. Phase 11 candidate scopes

### Path A — Golden-query suite expansion + Section 04i audit (medium)

1. Audit the six hallucination layers against the canonical doc;
   write a coverage matrix listing what each layer enforces vs the
   doc's intent.
2. Extend `test_golden_queries.py` with 5-10 additional scenarios
   covering: NI 43-101 lookups, mixed-corpus retrieval, numerical
   claim verification, citation-required edge cases.
3. Add a golden-query smoke verifier to the master sweep that runs
   one canary query end-to-end + asserts citations are present
   and traceable.

**Effort:** ~5 steps. **Value:** validates the existing framework
under realistic load, surfaces drift in the hallucination layers.

### Path B — Frontend Search/Query page (medium-large)

1. Create `Search.tsx` Inertia page with a question input,
   submit button, streamed-answer rendering, and citation chips
   linked to the chunk store.
2. Hook to the existing `v1/queries/{queryId}/start` endpoint;
   poll for completion via the orchestrator's existing state machine.
3. Test fixtures + Cypress / Playwright snapshot tests.

**Effort:** ~6 steps including TS types + tests. **Value:** turns
the backend RAG capability into something operators / pilot
customers can actually use through the browser.

### Path C — `prompts/` subdirectory canonicalisation (small)

1. Audit where prompt strings currently live (mostly inline in
   `orchestrator.py` + other agent files).
2. Refactor into `src/fastapi/app/agent/prompts/*.py` with one file
   per agent role + a `_SYSTEM_PROMPT_VERSION` registry.
3. Activate the Phase 5 Step 3 pre-commit hook end-to-end.

**Effort:** ~3 steps. **Value:** closes the prompt-version-bump
guard rail; modest. Pairs well with Path A.

### Recommended Phase 11

**Path A + Path C** as a combined "validation + discipline" phase
(~7 steps). Defers Path B (frontend Search page) to Phase 12 when
the backend has audited golden coverage.

---

End of scoping doc.
