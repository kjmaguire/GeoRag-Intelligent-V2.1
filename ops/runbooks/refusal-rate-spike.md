# Refusal-rate spike — triage on Azure

**What this is.** The system started saying "I can't answer that" (or
answering with nothing to cite) far more than it did last week. Refusing is
the *correct* behaviour when retrieval comes back empty or a hallucination
guard fires — hard rules 4 and 5 in `CLAUDE.md` — so the job here is not to
make refusals stop. It is to find out **why the inputs to those guards
changed**, which is almost always infrastructure or data, and only rarely
the guards themselves.

Written 2026-09-06 for Azure Container Apps. The compose-era version in
`_archived/` keys on a Prometheus alert and a `reason_code` enum that do not
exist in this deployment; its reasoning about what a spike *means* is still
sound and is folded in below.

```bash
az account set --subscription d314ab40-b5b7-4e3e-8308-86023fb7638a
RG=georag
PG="host=georag-pg-cc.postgres.database.azure.com dbname=georag user=georag_admin sslmode=require"
```

---

## 0. How you found out, and what the signal actually measures

There is no real-time refusal metric. `/metrics` on fastapi-cc exposes
`georag_out_of_scope_refusals_total` and
`georag_hallucination_guard_layer_fires_total{layer,outcome}`, and nothing
scrapes them (no Prometheus on Azure). The one automated signal is
**`answer_quality_watch`** (`app/hatchet_workflows/answer_quality_watch.py`),
a Hatchet cron at `30 14 * * *` UTC — just after the startup sweep — that
reads `silver.answer_runs` and compares **yesterday** against the **seven
days before it** on four things:

| signal | column | fires when |
| --- | --- | --- |
| refusal rate | `rejection_reason IS NOT NULL` | +15 percentage points |
| guard-fire rate | `hallucination_guard_results -> 'guards'` non-empty | +15 pp |

> **As built:** `persist_node` in `agent/agentic_retrieval/nodes.py`
> writes both columns since 2026-09-07. Before that date nothing wrote
> them, so rows older than that read as accepted with no guard results,
> and the first two signals could never fire. Do not compare a window
> that straddles 2026-09-07 against one that does not.
| zero-evidence rate | no rows in `silver.answer_retrieval_items` | +15 pp |
| mean confidence | `confidence` | −0.15 |

It refuses to compare unless **both** windows have at least 20 runs
(`insufficient_sample`), because three queries and one refusal is 33% and
means nothing. When something moved it logs one line containing
`ANSWER_QUALITY_REGRESSION` on hatchet-worker-cc, and the Azure Monitor
rule `answer-quality-regression` (`deploy/azure/alerts/create-alerts.sh`)
emails `georag-alerts-ag`. So:

- The alert is **a day late by design**. What it names happened yesterday.
- It is absolute, not relative: 2% → 6% does not fire; 20% → 35% does.
- It does not know whether answers are *correct* — only that the system's
  own signals moved. Correctness needs the golden set (§6).

The other ways you find out: a user says so, or `laravel-octane-cc-5xx`
fires (a spike of `system_error`-shaped refusals often surfaces there
first). Read the marker line before anything else — it names the metric,
both values and the threshold:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(3d)
| where ContainerAppName_s == "hatchet-worker-cc"
| where Log_s has "ANSWER_QUALITY_REGRESSION" or Log_s has "answer_quality_watch"
| project TimeGenerated, Log_s
| order by TimeGenerated desc
```

```bash
az monitor log-analytics query --workspace workspace-georag4ad7 --analytics-query "<the query above, on one line>" -o table
```

---

## 1. Ten-minute triage: what kind of spike is this?

All four signals are persisted, with history, on `silver.answer_runs`.
That is more useful than a counter at 3am, because it answers *when did
this start* and *which reason*. Run these with `psql "$PG"`. The tables
carry `FORCE ROW LEVEL SECURITY`; if a query returns nothing on a day you
know had traffic, your role is being filtered — use one with `BYPASSRLS`
or set the tenant GUC the way `docs/RUNBOOK.md` § "If you must bypass
Eloquent" describes.

**1a. When did it start?** Refusal and zero-evidence rate per hour, last
three days:

```sql
SELECT date_trunc('hour', ar.created_at)                                   AS hour,
       count(*)                                                            AS runs,
       count(*) FILTER (WHERE ar.citation_lifecycle_state = 'rejected')    AS refused,
       count(*) FILTER (WHERE ar.hallucination_guard_results IS NOT NULL
                          AND ar.hallucination_guard_results <> '{}'::jsonb) AS guard_fired,
       count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM silver.answer_retrieval_items i
                                          WHERE i.answer_run_id = ar.answer_run_id)) AS zero_evidence,
       round(avg(ar.confidence)::numeric, 3)                               AS mean_conf
FROM silver.answer_runs ar
WHERE ar.created_at >= now() - interval '3 days'
GROUP BY 1 ORDER BY 1 DESC;
```

A sharp edge at one hour is a deploy, a Foundry event or the database
coming back wrong after the maintenance window. A slow ramp over days is
data: a new corpus, a re-embed, a collection degrading.

**1b. Which reason?** `rejection_reason` is written once at INSERT by
`persist_node`. Its first token is the code to group on: the terminal
repair strategy's `reason_code` (`MISSING_ASSAY_UNITS`,
`AMBIGUOUS_HOLE_ID`, …) when one was stamped on the `refusal_payload`,
otherwise `insufficient_evidence` for a run with no real citation; the
other guard codes follow in parentheses, e.g.
`insufficient_evidence (guards: NO_EVIDENCE_FOUND, CITATION_INCOMPLETE)`.
Group on the opening word:

```sql
SELECT left(regexp_replace(rejection_reason, '[0-9a-f-]{36}|\d+', '#', 'g'), 70) AS reason,
       count(*) FILTER (WHERE created_at >= now() - interval '1 day')   AS last_24h,
       count(*) FILTER (WHERE created_at <  now() - interval '1 day')   AS prior_7d
FROM silver.answer_runs
WHERE created_at >= now() - interval '8 days' AND rejection_reason IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC LIMIT 25;
```

Map what you see onto the causes in §2–§5:

| reason text looks like | it is | go to |
| --- | --- | --- |
| LLM health probe failed / backend unavailable / timeout | the answer path could not reach Foundry or Postgres | §2 |
| out of scope / classifier refused, `confidence = 0.0`, no evidence | the fast out-of-scope path — real off-topic traffic or a routing regression | §5 |
| no evidence / retrieval returned nothing / below threshold | retrieval produced nothing worth citing | §3 |
| typed output / citation / numeric / entity / constraint guard | the §04i guards rejected a drafted answer | §4 |

**1c. Which guard?** When `guard_fired` is up, the JSONB says which layer:

```sql
SELECT k AS guard, count(*) AS fires
FROM silver.answer_runs ar, jsonb_object_keys(ar.hallucination_guard_results) k
WHERE ar.created_at >= now() - interval '1 day'
  AND ar.hallucination_guard_results IS NOT NULL AND ar.hallucination_guard_results <> '{}'::jsonb
GROUP BY 1 ORDER BY 2 DESC;
```

**1d. Is it one workspace?** A single project spiking while the rest are
flat is that project's data, not the platform:

```sql
SELECT workspace_id, project_id, count(*) AS runs,
       count(*) FILTER (WHERE citation_lifecycle_state = 'rejected') AS refused
FROM silver.answer_runs
WHERE created_at >= now() - interval '1 day'
GROUP BY 1, 2 ORDER BY refused DESC LIMIT 10;
```

Every rejected run also has a Retrieval Inspector page (the deep link the
UI shows on a refusal; API in `routers/answer_runs.py`) that renders the
same row with `guards_triggered` and what retrieval returned. For a
single bad answer that is faster than SQL.

---

## 2. Infrastructure: the answer path could not run

Check these in order; each one produces refusals that look like quality
problems and are not.

**Postgres.** If it is `Stopped` outside 06:00–14:00 UTC, or the app tier
came up against a stopped server, every query fails its health probe
(`azure-oncall.md` §1 — start it, then restart the consumers).

```bash
az postgres flexible-server show -g $RG -n georag-pg-cc --query state -o tsv
```

**Foundry.** One resource serves the LLM, Embed v4, Rerank v4 and Parse v5.
Throttling, quota exhaustion or a bad key hits all of them, and a spike of
`ClientErrors` explains a refusal spike on its own. `azure-oncall.md` §4
has the metric query and the false-positive caveat (a big scanned ingest
sends one Parse call per page). If `ClientErrors` is clean, check
`ServerErrors` the same way.

**Qdrant.** Two distinct failures:

- The collection lost points. `embed_pending_passages` logs
  `QDRANT_PARTIAL_LOSS` on hatchet-worker-cc when Postgres has passages the
  collection does not; the `qdrant-partial-loss` alert rule watches it.
  Retrieval then returns nothing for exactly the documents that are
  missing, which is a zero-evidence spike scoped to one project.
- The optimizer is stuck on a full SMB share (`azure-oncall.md` §4,
  `qdrant-cc-optimizer-stuck`). Searches still answer but slowly and
  incompletely.

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(2d) and ContainerAppName_s == "hatchet-worker-cc"
| where Log_s has "QDRANT_PARTIAL_LOSS"
| project TimeGenerated, Log_s | order by TimeGenerated desc
```

**A rotated secret that missed a consumer.** A 401 storm between apps
(`secret-rotation.md` §3 query) refuses everything on the Laravel side
and looks like `system_error`.

If any of these is red, fix it, then re-run §1a tomorrow: the watch
compares whole days, so it will keep firing until a clean day is inside
the current window.

---

## 3. Retrieval: nothing worth citing came back

Zero-evidence and "below threshold" refusals with Foundry and Qdrant
healthy mean the retrieval stack disagrees with itself. The usual reasons,
most likely first:

**Embedding model mismatch between ingest and query.** `EMBEDDING_BACKEND`
must be identical on fastapi-cc (query side) and hatchet-worker-cc
(ingest side), and both default to `foundry` since 2026-09-06. A worker
still on `local` embeds new passages with Qwen3 while queries embed with
Cohere Embed v4; both are 1024-dim so nothing errors — the vectors are
just unrelated, and every query returns junk below the reranker floor.
Switching backends **requires a full re-embed**
(`scripts/reset_embeddings_for_reencode.py`, then `embed_pending_passages`).

```bash
for app in fastapi-cc hatchet-worker-cc; do
  echo "== $app"
  az containerapp show -g $RG -n "$app" --query "properties.template.containers[0].env[?name=='EMBEDDING_BACKEND' || name=='RERANKER_BACKEND' || name=='AZURE_FOUNDRY_EMBED_DEPLOYMENT' || name=='AZURE_FOUNDRY_RERANK_DEPLOYMENT'].{k:name,v:value}" -o table
done
```

Any difference between the two apps is your answer. The
`meta.model_stack` fingerprint that `run_golden_benchmark.py` records
exists for this comparison.

**The reranker floor.** Retrieval quality gate as built is a flat reranker
score floor: `RERANKER_SCORE_THRESHOLD_FOUNDRY` (default 0.2) on Foundry,
`RERANKER_SCORE_THRESHOLD` (default 0.0) self-hosted, and
`RETRIEVAL_QUALITY_THRESHOLD` (default 0.5) downstream. Cohere Rerank v4
scores are not on the same scale as the Qwen3 cross-encoder's; a floor
tuned on one is wrong on the other. See what is being cut:

```sql
SELECT date_trunc('day', ar.created_at) AS day,
       count(*) AS runs,
       round(avg(cnt)::numeric, 1) AS mean_items,
       count(*) FILTER (WHERE cnt = 0) AS zero_items
FROM silver.answer_runs ar
LEFT JOIN LATERAL (SELECT count(*) AS cnt FROM silver.answer_retrieval_items i WHERE i.answer_run_id = ar.answer_run_id) c ON true
WHERE ar.created_at >= now() - interval '8 days'
GROUP BY 1 ORDER BY 1 DESC;
```

If mean items collapsed on the day the spike started and no env changed,
look at what was *ingested* that day.

**What was ingested.** New scanned documents whose pages fell back to
Tesseract (Parse unavailable, over the per-document page budget, or a
worker still pointed at the retired Document Intelligence engine) produce
passages with no table structure and weak text. Queries about those
documents refuse.

```sql
SELECT date_trunc('day', created_at) AS day, ocr_method, count(*)
FROM silver.document_passages
WHERE created_at >= now() - interval '8 days' AND ocr_method IS NOT NULL
GROUP BY 1, 2 ORDER BY 1 DESC, 3 DESC;
```

`tesseract` where `cohere_parse` was expected is ADR-0019's fallback
doing its job — see `azure-oncall.md` §4 for why Parse was down.

---

## 4. Guards: answers were drafted and rejected

A guard-fire spike with retrieval healthy means the LLM is producing
answers the validators will not accept: claims without a
`source_chunk_id`, numbers the evidence does not contain, entities that do
not resolve, geological constraints violated. Four guards run in
`app/agent/hallucination/orchestrator_validators.py` (typed output, numerical, entity,
constraints; completeness is advisory). §1c says which one.

Almost always this follows a **model or prompt change**: a Foundry
deployment retired or swapped (`AZURE_FOUNDRY_DEPLOYMENT`; Command A+ is a
Preview SKU with a listed retirement date), a prompt edit in a deploy, or
`LLM_BACKEND` flipped to the Anthropic fallback. Correlate with CD:

```bash
az containerapp show -g $RG -n fastapi-cc --query "properties.template.containers[0].{image:image,llm:env[?name=='LLM_BACKEND'].value|[0],deployment:env[?name=='AZURE_FOUNDRY_DEPLOYMENT'].value|[0]}" -o table
az containerapp revision list -g $RG -n fastapi-cc --query "[].{name:name,created:properties.createdTime,active:properties.active}" -o table
```

If the spike began with a revision, `azure-oncall.md` §5 rolls the image
back; that is the one case where a rollback is the right response to a
refusal spike. If the deployment changed underneath you, the golden set
(§6) is how you decide whether the new model is acceptable.

---

## 5. Out-of-scope: the fast refusal path

When the LLM classifier judges a query entirely off-topic, the
orchestrator refuses before drafting and writes a rejected row with
`confidence = 0.0` and no evidence. A rise here is one of three things:

- **Users asking the wrong thing** — one workspace, varied phrasing. A UX
  problem (a "what can I ask?" hint), not an incident.
- **Many users, the same phrase** — something in the UI is producing it.
- **A burst from one source** — probe or abuse; check the Laravel access
  logs for the origin and throttle.

Only if in-scope questions are being refused is it a routing regression:
the keyword classifier escalating to the LLM too eagerly, or the
classifier prompt changed. That is a code fix, not a threshold.

---

## 6. Prove it, then decide

Before changing a threshold or declaring a model regression, run the
golden set against the **live** backend from inside fastapi-cc (this
spends Foundry tokens):

```bash
az containerapp exec -g $RG -n fastapi-cc --command 'python3 /app/scripts/run_golden_benchmark.py --question-set refusal_correctness --label spike-$(date -u +%Y%m%d)'
az containerapp exec -g $RG -n fastapi-cc --command 'python3 /app/scripts/run_golden_benchmark.py --per-set 2 --label spike-$(date -u +%Y%m%d)-coverage'
```

`compare_benchmarks.py` diffs two reports and refuses to compare mismatched
model stacks. There is **no committed post-Cohere baseline** yet (L423):
the first spike you triage this way produces the baseline the next one is
measured against — commit it. The nightly `eval-gate.yml` runs with LLM
and embeddings stubbed and cannot see any of this.

`docs/RUNBOOK.md` § "Golden-set flywheel" mines last week's refusals for
golden-set candidates; a spike is the right time to run it.

---

## 7. What not to do

- **Do not lower `RERANKER_SCORE_THRESHOLD_FOUNDRY` or
  `RETRIEVAL_QUALITY_THRESHOLD` below their defaults** without the SME.
  They are hallucination guards; loosening them turns refusals into
  confident wrong answers, which is the failure this whole design exists
  to prevent.
- **Do not disable a guard or a citation check** to make a number go
  down. Refusing beats fabricating, every time.
- **Do not roll back the deploy** unless §4 tied the spike to a revision.
  Most spikes are data or infrastructure, and a rollback just delays
  finding out which.
- **Do not act on an `insufficient_sample` day.** The watch declining to
  compare is correct behaviour on a quiet week, not an alert.

---

## 8. Record it

```bash
az containerapp exec -g $RG -n laravel-octane-cc --command "php artisan tinker --execute 'Log::channel(\"authz_audit\")->info(\"triage_action\", [\"incident\" => \"refusal_rate_spike\", \"actor\" => \"<you>\", \"cause\" => \"<what §1-5 found>\", \"action_taken\" => \"<what you changed>\"]);'"
```

Then check tomorrow's `answer_quality_watch` line. The spike is over when
the current window is inside the thresholds again with a real sample, not
when the alert stops emailing.

---

## Cross-references

- `ops/runbooks/azure-oncall.md` — §1 Postgres, §3 worker consuming, §4 Foundry / Qdrant, §5 rollback, §6 reading logs.
- `ops/runbooks/secret-rotation.md` — §3 for the 401 storm a half-rotated key produces.
- `app/hatchet_workflows/answer_quality_watch.py` — the watch, its thresholds and its sample gate.
- `app/agent/hallucination/orchestrator_validators.py` — the four guards as built; `georag-architecture.html` §04i for the six-layer design they implement.
- `deploy/azure/alerts/create-alerts.sh` — `answer-quality-regression`, `qdrant-partial-loss`, the Foundry rules. `qdrant-cc-optimizer-stuck` predates the script and lives only in the portal.
- `src/fastapi/scripts/run_golden_benchmark.py`, `compare_benchmarks.py` — the correctness measurement this runbook cannot give you.
- `_archived/refusal-rate-spike.md`, `_archived/retrieval-tuning.md` — compose-era; the reasoning holds, the commands do not.
