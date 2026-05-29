# Phase H3 — PGEO + §7 dashboards + §5 strip logs + §11 DR runbooks

**Status:** ✅ Four deliverables shipped. 1151/1151 unit suite pass.
Eval 20-22/22 (LLM-determinism range).

## What landed

### 1. PGEO cache roundtrip — close the H2 carry-over

`search_public_geoscience` now serialises into the auxiliary slot
with all nested `PublicGeoscienceRecord` data (commodities, license
notes, BBOX, source_url, staleness). The H2 partial-source fallback
for PGEO is gone — PGEO queries now hit the cache cleanly.

Files:
* `app/agent/orchestrator/run_cache.py` — `_AUXILIARY_TOOL_NAMES`
  bumped to include `search_public_geoscience`; `_rehydrate_auxiliary`
  extended to reconstruct `PublicGeoscienceSearchResult` (with nested
  record list).
* `app/agent/orchestrator/__init__.py` — partial-source check no
  longer special-cases PGEO; the unified auxiliary-slot guard
  handles it.
* `tests/test_run_cache_rehydration.py` — +2 PGEO tests
  (single-record roundtrip + zero-record roundtrip).

### 2. §7 workflow-tier Grafana dashboards (§16.2 — 5/5)

Production-grade dashboard JSON for the 5 §16.2 panels. Total: **46
panels** across 5 dashboards.

| Dashboard | Panels | Focus |
|---|---|---|
| `georag-workflows-hatchet.json` | 7 | AI + ingestion pool throughput, failure rate, retries, registration health |
| `georag-workflows-dagster.json` | 8 | Asset materialisation latency / success / freshness, data_version bumps, sensor health |
| `georag-workflows-kestra.json` | 6 | Flow throughput by namespace, state distribution, dispatch latency, failure reasons, queue depth |
| `georag-workflows-llm-pipeline.json` | 8 | LLM calls/min, cache hit rate, calls-per-query histogram, failovers, partial-tool failures, chunks returned, budget exhaustion |
| `georag-workflows-cost-burn.json` | 8 | 1h/24h/7d/30d stat panels, $/min by model + user_bucket, input vs cache_read, $/query trend |
| `georag-workflows-outbox.json` | 9 | Outbox pending rows, dispatch latency, Activepieces flow state, audit ledger anchor rate, hash-chain integrity, queue depths |

All panels reference metrics that already exist in `app/metrics.py`
or are emitted by the Hatchet / Dagster / Kestra runtimes. Some
require a Prometheus scrape job for the Dagster + Kestra exporters
(operator-side config; tracked in `phase_h_python_deps_audit.md`).

### 3. §5 strip-log starter

The first §5 visualisation surface. Foundation for all subsequent
drillhole visuals (cross-sections + stereonets follow the same
pattern).

* `database/raw/phase5/10-drillhole-intervals-visual.sql` — the
  gold table schema with RLS + per-collar-depth + per-project +
  partial-mineralised indexes. Materialised by a future Dagster
  asset; the SQL is ready for the operator to apply.
* `app/services/visualizations/strip_log.py` — pure-function
  renderer with TWO output paths:
  - `render_strip_log_plotly_figure()` — JSON dict for react-plotly.js
    interactive embed
  - `render_strip_log_matplotlib_png()` — static PNG bytes for the
    Report Builder PDF + data-room exports
  Both consume the same `StripLogInterval` dataclass list (matches
  the gold table row shape one-to-one).
* SME-curated default lithology palette (`SST`, `CGL`, `PGN`, `GPT`,
  etc.); per-row `display_color` overrides the palette.
* Mineralised intervals get a thicker dark-green stroke in both
  outputs.
* Empty-input contract: returns a clean "no data" figure / PNG
  with a clear annotation; no exception raised.
* `tests/test_strip_log_renderer.py` — 21 pure-function tests
  covering: interval-length property, label resolution fallback
  chain, empty input, three-interval happy path, depth-axis
  reversal, mineralised stroke, palette application, explicit
  display_color override, unknown-code fallback, tick labels,
  unsorted-input handling, hover text, layout sizing, matplotlib
  PNG header + size sanity, DPI parameter, single-interval edge
  case.

### 4. §11 DR runbooks — 5 production-shape

Upgraded from doc-phase 104 skeletons to procedure-ready operator
documents. Each runbook now includes:
- Detection signals (with their Prometheus / log / SQL source)
- RTO/RPO tier breakdown
- Phase A-E concrete procedure with **executable docker compose +
  SQL commands**
- Verification steps
- Post-mortem template anchor

| Runbook | Scope |
|---|---|
| `dr-1-postgres-loss.md`     | Postgres corruption / WAL break / dropped table |
| `dr-2-store-divergence.md`  | Cross-store drift (Postgres intact; Neo4j/Qdrant/SeaweedFS/Redis re-projected from silver) |
| `dr-3-ransomware.md`        | Adversarial tampering / hash-chain break / encrypted blobs — STRICTEST scenario, restoration from immutable signed cold-tier |
| `dr-4-full-datacenter.md`   | Region loss, streaming-replica promote + DNS cutover |
| `dr-5-partial-outage.md`    | Single-component degradation; decision-tree mapping symptom → bypass flag / restart command (most common) |

Every runbook references the G.2 `restore_workspace` workflow as
the cross-store consistency engine. dr-5 cross-references the §G.5
support cockpit + dispatcher work for monitoring + alerting.

## Verification

* **Backend unit suite:** 1151 passed / 0 failed / 24 skipped /
  79 deselected (up from 1128; +23 new tests across PGEO and
  strip-log).
* **22-question eval:** 22/22 cold; 20-22/22 warm — same LLM-
  determinism range as previous batch.
* **24/24 containers healthy.**
* **Grafana JSON valid** for all 5 new dashboards (validated via
  `python -c "import json; json.load(...)"`).

## What's still open

| Next pull-task | Source |
|---|---|
| Dagster asset to materialise `gold.drillhole_intervals_visual` | follow-up to this strip-log starter |
| FastAPI router `/internal/v1/viz/strip_log` | a 30-min wire-up; route exists; needs db fetch + renderer dispatch |
| Inertia React page: Drillhole Detail with embedded strip log | frontend-engineer work |
| Cross-section + stereonet renderers | §5.7 / §5.8 same shape as strip log |
| §16.1 product-tier dashboards (8 remaining) | needs domain product input |
| §16.3 ops-tier dashboards (9; Grafana provisioning) | mostly config + already-existing scrapes |
| §11.6 Helm chart + §11.7 k8s manifests | operator-tier |
| §11.8 air-gapped bundle pipeline | operator-tier |
| DR rehearsal drills | needs operator + staging environment |

## Files

* New: 1 SQL substrate (`database/raw/phase5/10-drillhole-intervals-visual.sql`)
* New: 2 visualization modules
  (`app/services/visualizations/__init__.py` +
  `strip_log.py`)
* New: 5 Grafana dashboards (`docker/grafana/dashboards/georag-workflows-*.json`)
* New: 1 strip-log test file (21 tests)
* Updated: 5 DR runbooks (skeleton → production-shape)
* Updated: `app/agent/orchestrator/run_cache.py` (PGEO roundtrip)
* Updated: `app/agent/orchestrator/__init__.py` (PGEO partial-source
  fallback removed)
* Updated: 2 cache rehydration tests (+2 PGEO roundtrip tests)
* New: `docs/phase_h3_pgeo_dashboards_striplog_dr.md` (this doc)
