# Appendix J — Testing + Evaluation Matrix

Status: **Draft.** Defines what "tested" means for every feature class.

## 1. Test layers

| Layer | Tool | Where | Runs in CI |
|---|---|---|---|
| **PHP unit** | PHPUnit | `tests/Unit/**` | every PR |
| **PHP feature** | PHPUnit + `RefreshDatabase` | `tests/Feature/**` | every PR |
| **Inertia component** | Vitest + React Testing Library | `resources/js/Pages/__tests__/**`, `resources/js/Components/__tests__/**` | every PR |
| **Browser e2e** | Playwright | `playwright/**` + `tests/Browser/**` (Dusk legacy) | every PR (smoke), nightly (full) |
| **Python unit** | pytest | `src/fastapi/tests/unit/**`, `src/dagster/tests/unit/**` | every PR |
| **Python integration** | pytest + ephemeral PG/Qdrant/Neo4j | `src/fastapi/tests/integration/**` | every PR |
| **pgTAP** | pgTAP | `database/tests/pgtap/**` | every PR |
| **RAG golden** | Hatchet `eval_real_rag_nightly` | `eval/golden/*.yaml` | nightly |
| **Load** | k6 | `docs/load_tests/**` | weekly |
| **Failure / chaos** | custom (toxiproxy) | `tests/Chaos/**` (planned) | weekly |
| **Acceptance** | per-phase `phase-verify` skill | `docs/*kickoff*.md` checklists | on-demand at milestone gate |

## 2. Per-feature test contracts

### 2.1 Tenant isolation (RLS)

- `tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php` ([file](../../../tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php)) — every workspace-scoped table has RLS enabled + at least one policy.
- Per-table: write rows under workspace A + B; assert workspace A query
  with `app.workspace_id=A` never returns workspace B rows AND vice
  versa.
- **Pass criteria**: 100 % of tables with `workspace_id` are policy-covered.

### 2.2 PDF ingestion

- `src/fastapi/tests/integration/test_ingest_pdf_e2e.py` — golden PDFs in
  `tests/fixtures/pdf/`.
- Per fixture: assert `silver.reports` row, `silver.report_pages` count
  equals expected, OCR confidence ≥ threshold, `bronze.provenance` paired.
- `silver.parser_run_artifacts` has rows for preflight + parse + persist.
- **Pass criteria**:
  - All fixtures process without error.
  - p95 parse time ≤ baseline +20 %.
  - Per-page char count ≥ expected for native-text fixtures.

### 2.3 CSV / XLSX parsers

- `src/dagster/tests/unit/test_csv_*.py` — per parser file.
- Round-trip: known CSV → bronze row → silver row → recoverable from
  silver alone (text + provenance).
- Decimal-comma / delimiter auto-detect / vendor alias substitution tests.
- **Pass criteria**: each CSV format in [Appendix E](E-ingestion-format-matrix.md)
  has at least one golden fixture + replay test.

### 2.4 Geospatial

- pgTAP `database/tests/pgtap/08_silver_mvt_functions.sql` — every
  `silver.pg_*_by_project` function: argument types, return signature
  `(mvt bytea, etag_hash text)`, deterministic ordering.
- pgTAP `10_golden_mvt_snapshots.sql:152` — bit-identical MVT bytes for
  fixed input.
- CRS round-trip: write a row in EPSG:4326 → assert geometry survives
  reprojection.
- **Pass criteria**: zero pgTAP failures.

### 2.5 Knowledge graph

- `tests/Feature/Graph/SchemaBootstrapTest.php` — runs `init-schema.cypher`
  on an ephemeral Neo4j; asserts all uniqueness constraints + indexes
  exist.
- Workspace fence test: nodes in workspace A + B; query with `$ws=A`
  returns A only.
- Idempotent `MERGE`: run an upsert block twice; assert node and edge
  counts unchanged.
- Tombstone test: `:Tombstoned` label causes read tools to skip.
- **Pass criteria**: schema present after every CI run; fence violations
  emit `cross_workspace_leak` audit row.

### 2.6 RAG golden queries

- Suite location: `eval/golden/<workspace_template>/*.yaml`.
- 120 queries per template (gold, base metal, uranium SK).
- Per query:
  - `pass.citation_count` ≥ 1
  - `pass.all_citations_resolve` = true
  - `pass.no_layer3_or_6_demote` = true
  - `pass.top3_recall` ≥ 0.80 (vs. labelled corpus)
  - `pass.reranker_top_score` ≥ 0.50
- **Aggregate pass criteria** (milestone gate):
  - Citation coverage ≥ 95 %.
  - Refusal-on-bad-input ≥ 90 %.
  - Hallucination-block ratio ≥ 99 %.

### 2.7 Citation invariants

- `tests/Feature/Citations/CitationLifecycleTest.php`:
  - Every `[ev:xxxxxxxx]` in an answer maps to a real `silver.evidence_items.evidence_id`.
  - `silver.answer_citation_items.citation_lifecycle_state` transitions
    only via the documented states (`pending → resolved`, etc.).
- **Pass criteria**: 0 unresolved markers in the nightly golden suite.

### 2.8 Numeric correctness

- `tests/Feature/Rag/NumericClaimTest.php`:
  - Synthetic answers with deliberate numeric distortions → Layer 3
    rejects.
  - Real golden answers → Layer 3 passes.
- **Pass criteria**: 100 % rejection of distorted claims; <2 % false
  positive on real claims.

### 2.9 Load

- k6 scripts in `docs/load_tests/`:
  - `chat_query.k6.js` — 30 concurrent users, 10-min run, target
    p95 < 8 s end-to-end.
  - `ingest_pdf.k6.js` — 5 concurrent uploads, target completion < 90 s
    per 50-page PDF.
  - `tile_request.k6.js` — 200 RPS sustained, target p95 < 200 ms.
- **Pass criteria** (weekly gate): no regression beyond +20 % vs the
  previous baseline.

### 2.10 Failure / recovery

- Toxiproxy-driven chaos tests (planned, `tests/Chaos/`):
  - Postgres down 30 s → Hatchet `ingest_pdf` retries succeed.
  - Qdrant down 30 s → search refuses (no fake hits), recovers.
  - vLLM down → cross-backend failover engages (when enabled).
  - Reverb down → chat falls back to polling (degraded UX), no data
    loss.
- **Pass criteria**: documented refusal vs. retry on each failure mode.

### 2.11 Backup + restore

- `tests/Feature/Backups/RestoreSmokeTest.php` (planned):
  - Take a PG base + WAL stream.
  - Restore into an ephemeral PG.
  - Assert workspace count + RLS coverage + a known audit hash matches.
- **Pass criteria**: end-to-end restore in < 60 min on a 100 GB DB.

## 3. Acceptance — per-phase pass criteria

Driven by the [phase-verify](../../../.claude/skills/phase-verify) skill.
The "Definition of Done" lives in each phase's kickoff doc:

| Phase | Doc | Highlights |
|---|---|---|
| Phase 0 | `docs/phase0_handoff.md` | 10 extensions installed, 8 schemas present, audit chain verifier returns intact |
| Phase 1 | `docs/phase1_geologist_question_plan.md` | OIUR flag flippable; smoke 1.2–1.5 green |
| Phase 2 | `docs/phase2_geologist_question_plan.md` | Agentic LangGraph 6-intent classifier returns labelled intent for golden queries |
| Phase 3 | `docs/phase3_geologist_question_plan.md` | Field/Office context envelope + Laravel↔FastAPI bridge integration test |
| Phase 4 | `docs/phase4_geologist_question_plan.md` | §04e expansion green; anomaly subgraph degrades gracefully |

## 4. Test-DB parity

Pre-existing convention from
[notes/INDEX.md#project_test_db_parity_gap](../notes/INDEX.md#project_test_db_parity_gap):
every raw-SQL migration ships a sibling `*_provision_*_for_test_db.php`
mirror. `EXEMPT_TEST_DB_ONLY_TABLES` is empty as of 2026-05-25.

## 5. Coverage gates

| Suite | Minimum coverage |
|---|---|
| PHP unit + feature | line 80 % project-wide, branch 70 % |
| Python unit | line 75 %, async paths fully covered |
| Inertia component | every prop type at least visited |
| pgTAP | every MVT function has a golden snapshot |
| RAG golden | 120 queries × 3 templates = 360 queries pass the aggregate gate |

## 6. CI orchestration

`.github/workflows/ci.yml` (existing):
- PHP: composer install → migrate → pint check → phpstan → phpunit.
- JS: npm ci → typecheck → eslint → vitest → playwright (smoke).
- Python: uv sync → ruff → mypy → pytest -k "not load and not chaos".
- pgTAP: docker compose up ephemeral PG → apply migrations → pgtap.

Nightly `nightly.yml`:
- Full Playwright suite.
- k6 load suite.
- RAG golden eval.
- Backup restore smoke.
- Chaos tests (when implemented).

## 7. Test data + fixtures

- PDF fixtures: `tests/fixtures/pdf/` — copies of small NI 43-101s with
  redacted PII.
- CSV fixtures: `tests/fixtures/csv/` — per format with edge cases
  (decimal-comma, multi-encoding).
- GPKG: `tests/fixtures/gpkg/` — small SK-area extract.
- Golden queries: `eval/golden/` — yaml with `query`, `expected_intent`,
  `expected_evidence_ids`, `expected_citations_count`,
  `expected_min_top3_recall`.
- Workspace seed: `database/seeders/AcceptanceWorkspaceSeeder.php` —
  one of each silver row kind + a few PDFs.

## 8. Forbidden test patterns

- No tests that disable RLS (`SET LOCAL row_security=off`).
- No tests that grant `BYPASSRLS` to `georag_app`.
- No tests that hit external networks (HuggingFace / Anthropic /
  provincial open-data endpoints) — use recorded fixtures.
- No tests that share state across files (each test class refreshes the
  database or uses scoped transactions).
