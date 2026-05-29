---
# Module 10 Chunk 10.7 — API latency baseline.
#
# These numbers are the regression threshold for the nightly perf-baseline.yml
# CI job. The job runs `src/fastapi/scripts/load_test.py --json` against
# secrets.STAGING_URL with concurrency=10 / total=30 and fails if any
# class p95 exceeds the value below by more than 20%.
#
# How to update
# -------------
# 1. Run a fresh measurement:
#      docker compose --profile dev-llm up -d
#      docker compose exec -T fastapi python scripts/load_test.py \
#          --concurrency 10 --total 30 \
#          --project 019d74a1-fba8-7165-9ae6-a5bf93eef97d --json \
#          > /tmp/load.json
# 2. Inspect /tmp/load.json — verify quality (no failures, no anomalies).
# 3. Replace the values in this YAML block with the measured p95_seconds.
# 4. Update the `measured_at` timestamp + commit message references this
#    chunk + the new commit SHA.
# 5. PR for review — Kyle approves before merge.
#
# Hardware reference
# ------------------
# Baseline was first authored without measured numbers (Chunk 10.7
# delivery — "first run pending"). Real numbers are populated on first
# successful nightly perf job after STAGING_URL is configured.

measured_at: "PENDING — first run after STAGING_URL secret is configured"
hardware: "TBD — record CPU model, RAM GB, GPU model+VRAM GB on first measurement"
concurrency: 10
total_queries: 30
seed: 42

baselines:
  # Conservative initial values per spec §05c. Bump or tighten on first
  # real measurement. Units: p50/p95/p99 are seconds; queries that hit
  # the LLM are slower than pure-retrieval queries.
  count:    {p50_seconds: 0.50, p95_seconds: 1.20, p99_seconds: 2.00}
  exists:   {p50_seconds: 0.40, p95_seconds: 1.00, p99_seconds: 1.80}
  numeric:  {p50_seconds: 0.80, p95_seconds: 1.80, p99_seconds: 3.00}
  spatial:  {p50_seconds: 1.00, p95_seconds: 2.50, p99_seconds: 4.00}
  document: {p50_seconds: 1.50, p95_seconds: 4.00, p99_seconds: 7.00}
  graph:    {p50_seconds: 1.20, p95_seconds: 3.00, p99_seconds: 5.00}
  refusal:  {p50_seconds: 0.30, p95_seconds: 0.80, p99_seconds: 1.50}
---

# API Latency Baseline — 2026-04-22

This document is the **release-gate threshold** for FastAPI query latency.
The nightly `perf-baseline.yml` CI job compares fresh load-test results
against the YAML frontmatter above and fails if any class p95 regresses
by more than 20%.

## Status: PENDING first measurement

The values in the YAML block are conservative initial estimates derived
from §05c expected-latency commentary in the architecture doc. They are
**not measured**. The first real measurement happens on the first nightly
run after `STAGING_URL` is configured (see `ops/runbooks/secret-management.md`).

When that first run completes:
1. The CI job uploads the JSON results as `perf-baseline-<run>` artifact.
2. An operator inspects the JSON, validates the run quality.
3. Operator opens a PR replacing the YAML values with the measured p95s.
4. From that PR forward, the nightly job is a real regression gate.

## Per-class methodology

The seven query classes from spec §05c are exercised by `load_test.py`
via fixtures from `src/fastapi/tests/test_golden_queries.py`. Each class
has ≥3 cases (Module 10 Chunk 10.3 enforced this via
`test_golden_query_class_coverage`). The harness rotates through the
fixtures uniformly, hits `/internal/queries`, and records wall-clock
latency.

### What "regression" means

A regression is a sustained increase in p95 — random variance is
expected (~5-10% jitter on a shared runner). The 20% threshold absorbs
jitter while catching:
- Slow rebase that introduces a synchronous DB call in an async path.
- Qdrant or Postgres index drift after a migration.
- LLM model swap that's slower per token.
- Network-route change that adds RTT.

### What "regression" does NOT mean

- Cold-start latency. The harness primes the model and runs total=30
  queries; first-query effects are smoothed by the p95 statistic.
- Single-query outliers. p99 is allowed to spike; p95 is the gate.
- Queue saturation under load. The harness uses concurrency=10 against
  a 4-worker FastAPI deployment — saturated by design but not OOM'd.

## Operator playbook

| Symptom | Likely cause | Triage |
|---------|--------------|--------|
| `count` p95 > baseline | Postgres index drift | `ANALYZE silver.collars; VACUUM (FULL, VERBOSE)` |
| `numeric` p95 > baseline | Aggregate query plan flip | Check pg_stat_statements top-10 |
| `spatial` p95 > baseline | GIST index bloat or PostGIS upgrade quirk | Reindex GIST + verify version |
| `document` p95 > baseline | Qdrant search slowdown or LLM throttle | Check Qdrant `qdrant-services` dashboard + Ollama queue depth |
| `graph` p95 > baseline | Neo4j page cache cold | Run APOC warmup script |
| `refusal` p95 > baseline | New refusal pipeline overhead | Profile `refusal_decision()` in flame graph |

## Capacity-planning context

See `ops/baselines/capacity-planning.md` for the "this stack supports N
concurrent users on hardware H" companion doc. The latency baseline is
**per-query**; capacity is the **per-second** budget at sustained load.

## Change log

- **2026-04-22 (Chunk 10.7)**: file created with conservative initial values; PENDING first real measurement.
