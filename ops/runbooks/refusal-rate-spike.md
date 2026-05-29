# Refusal-Rate Spike Triage

**Module 10 Chunk 10.8** — investigate sustained `out_of_scope_refusals_total` rate.
Pairs with the `GeoRAG — RAG Quality` dashboard's refusal panel.

## When this fires

- `FastAPIHighRefusalRate` Prometheus alert: `rate(out_of_scope_refusals_total[10m]) > 0.5` sustained 10 min.
- Operator-flagged elevation in the RAG Quality dashboard.
- Customer report: "the system is refusing things it should answer."

## Quick triage (3 minutes)

The refusal pipeline (Module 6) emits a refusal payload with a
`reason_code` enum from spec §10p:
- `out_of_corpus` — query doesn't intersect any indexed source.
- `not_found` — entity referenced doesn't exist.
- `quality_gate_failed` — retrieval crossed the threshold but no source met the citation guards.
- `numeric_no_evidence` — count/exists query but no rows matched.
- `system_error` — internal failure (LLM down, Qdrant unreachable).
- `corpus_boundary` — domain out-of-scope (e.g. medical question on a mining corpus).

```bash
# 1. Per-reason rate.
curl -sG http://localhost:9090/api/v1/query \
    --data-urlencode 'query=sum by (reason_code) (rate(out_of_scope_refusals_total[5m]))'
```

| Spike on | Likely cause |
|----------|--------------|
| `out_of_corpus` | Real corpus boundary OR ingestion gap (silver tables empty for a project) |
| `not_found` | UI bug producing entity references for entities that don't exist |
| `quality_gate_failed` | Retrieval threshold tightened too far OR Qdrant degraded |
| `numeric_no_evidence` | Real "no data" OR a recent migration dropped data |
| `system_error` | **Investigate immediately** — backend is broken |
| `corpus_boundary` | Likely real (out-of-scope query); spike may be malicious traffic |

## Triage by reason_code

### out_of_corpus or numeric_no_evidence

```bash
# Check if any silver tables are unexpectedly empty for the project.
docker compose exec -T postgresql psql -U georag -d georag <<'SQL'
SELECT 'collars'        AS table_name, count(*) FROM silver.collars        UNION ALL
SELECT 'drill_traces',          count(*) FROM silver.drill_traces           UNION ALL
SELECT 'evidence_items',        count(*) FROM silver.evidence_items         UNION ALL
SELECT 'document_revisions',    count(*) FROM silver.document_revisions     UNION ALL
SELECT 'project_boundaries',    count(*) FROM silver.project_boundaries     UNION ALL
SELECT 'historic_workings',     count(*) FROM silver.historic_workings;
SQL
```

If any table shows 0 rows when it shouldn't, the ingestion run failed
silently. Check Dagster recent runs:

```bash
docker compose exec dagster-webserver curl -s http://localhost:3001/health
# Open the Dagster UI at http://<host>:3001 → Runs → filter for failed/canceled.
```

### quality_gate_failed

The retrieval threshold (default `RETRIEVAL_QUALITY_THRESHOLD=0.6`) may
be too tight. Check what's currently failing:

```bash
# Open Loki Explore in Grafana with:
{service="fastapi"} | json | event="quality_gate_failed" | line_format "{{.query_text}} score={{.top_score}}"
```

If many queries are scoring 0.55-0.59 (just below threshold), consider
lowering the threshold OR investigating retrieval drift (Qdrant index
issue, BGE reranker drift).

```bash
# Temporarily lower for triage. NOT for production silently.
docker compose exec fastapi env | grep RETRIEVAL_QUALITY
docker compose exec laravel-octane php artisan tinker
>>> config(['fastapi.retrieval_quality_threshold' => 0.55]);
```

### system_error

Backend is broken. Goto `on-call.md` → SERVICE-OUTAGE branch.

The refusal pipeline emits `system_error` on:
- Qdrant timeout / connection refused.
- Neo4j unreachable.
- LLM (Ollama / vLLM) throwing 5xx.
- Pydantic AI typed-output validation hard-failure.

The Grafana service-health dashboard tells you which one.

### corpus_boundary

User asked something genuinely outside the corpus (e.g. medical-domain
question to a mining RAG). Distinguish:

- **Single user, repeated boundary refusals** → user education issue.
  Reach out via the customer success rotation if this is a paying tenant.
- **Many users, same boundary phrase** → possible UI prompt leak. Check
  if the SPA is auto-completing a query that crosses the boundary.
- **Burst from one IP** → security concern. Investigate IP, throttle if
  warranted.

## Coordinate with retrieval-pipeline runbook

Existing `ops/runbooks/retrieval-pipeline.md` covers the upstream
retrieval surface — refusal-rate spike triage points there for the
"why is retrieval scoring low?" debug path.

## What to NOT do

- **Don't lower the quality threshold below 0.5** without a Kyle decision.
  The threshold is a hallucination guard.
- **Don't disable refusals.** Refusing > making something up. Refusal is
  the correct behavior on a corpus boundary.
- **Don't roll back the deploy** for a refusal spike unless it correlates
  with a SHA AND impact is large. Refusal-rate spikes are usually data,
  not code.

## Audit trail

Like the authz triage, log triage actions:

```bash
docker compose exec laravel-octane php artisan tinker
>>> Log::channel('authz_audit')->info('triage_action', [
...     'incident' => 'refusal_rate_spike',
...     'actor' => 'kyle@example.com',
...     'action_taken' => 'lowered_threshold_from_0.6_to_0.55',
...     'reason' => 'qdrant_index_drift_post_ingest',
... ]);
```

## Cross-references

- `ops/runbooks/retrieval-pipeline.md` — upstream retrieval debug.
- `ops/runbooks/citation-pipeline.md` — citation guards that produce refusals.
- `docker/grafana/dashboards/georag-rag-quality.json` — refusal panel.
- `src/fastapi/app/services/citation_guards.py` — the guard logic that fires refusals.
- `src/fastapi/app/agent/orchestrator.py` — refusal payload assembly.
- §10p in the architecture doc — refusal taxonomy.
