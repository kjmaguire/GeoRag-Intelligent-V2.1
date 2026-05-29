# LLM Model Swap

**Module 10 Chunk 10.8** — rotate the live LLM cleanly with no user-visible
downtime (or, when downtime is unavoidable, manage it). Pairs with
`docs/model_migration.md` (which is the design doc; this is the
operator playbook).

## When to swap

| Trigger | Example |
|---------|---------|
| Quality regression on a model release | Anthropic deprecates a model; quality drops on benchmark |
| Cost change | Smaller distill is now sufficient for the corpus |
| Hardware change | Moved from L4 to L40S — can run a bigger model |
| Vendor outage | Anthropic API down; failover to local Ollama |
| Compliance | Mining client requires on-prem-only — swap from Claude API to Ollama |

## Pre-flight

```bash
# 1. Confirm the new model is available + tested in dev.
docker compose exec ollama ollama list
docker compose exec ollama ollama pull qwen3:30b-a3b-q4_K_M

# 2. Run the full golden + hallucination suite against the new model.
#    The release-rehearsal.yml workflow does this — trigger it explicitly.
gh workflow run release-rehearsal.yml \
    --field target=staging \
    --field llm_model=qwen3:30b-a3b-q4_K_M

# 3. Inspect the artifacts. Both suites must pass before swapping prod.

# 4. Check the LLM-cost dashboard — verify the new model's per-query
#    cost matches expectations.
```

## Rolling swap (preferred)

For Ollama-based deployments where multiple models can be loaded:

```bash
# 1. Pre-warm the new model on the LLM host (so the first real request
#    doesn't pay cold-start latency).
curl -X POST http://localhost:11434/api/generate \
    -d '{"model":"qwen3:30b-a3b-q4_K_M","prompt":"ready","keep_alive":"60m"}'

# 2. Update the model name in the encrypted env.
sops .env.production    # OLLAMA_MODEL=qwen3:30b-a3b-q4_K_M
sops --encrypt > .env.production.enc

# 3. Restart FastAPI workers one at a time (rolling).
#    For Octane / uvicorn: each worker reloads on SIGHUP.
docker compose exec fastapi sh -c 'pkill -HUP uvicorn'
sleep 30   # wait for the handoff
# Confirm new model active:
curl -s http://localhost:8000/health | jq .

# 4. Watch the RAG Quality dashboard for the next 30 min:
#    - refusal_rate stable
#    - p95 latency similar
#    - llm_cost_usd matches prediction
```

## Cold swap (when rolling isn't possible)

For vLLM with single-GPU pinning — only one model fits at a time:

```bash
# 1. Drain.
#    Set the load balancer / Laravel side to refuse new SSE streams.
#    Wait for in-flight queries to complete (Module 9 9.4
#    `MULTI_TENANT_ENFORCEMENT_ENABLED` doesn't gate this; it's a
#    deployment concern. Use Pulse to watch active streams drop to 0.)
docker compose exec laravel-octane php artisan pulse:work | grep sse_active

# 2. Stop vLLM, swap model, restart.
docker compose stop vllm
sops .env.production    # VLLM_MODEL=...
sops --encrypt > .env.production.enc
docker compose --env-file .env.production up -d vllm

# 3. Wait for vLLM to load (~3-10 min for a 70B model).
docker compose logs -f vllm | grep "model loaded"

# 4. Restart FastAPI to pick up the new model name.
docker compose restart fastapi

# 5. Resume traffic.
```

Cold swaps are 10-30 min. Schedule during maintenance windows.

## Provider-level swap

Switching from Ollama → vLLM, or local → Anthropic API, is more invasive
because the integration adapter changes (different httpx client, different
streaming protocol).

```bash
# 1. Update LLM_BACKEND in encrypted env.
sops .env.production
#   LLM_BACKEND=vllm      # was ollama
#   VLLM_URL=http://vllm:8000/v1
#   ANTHROPIC_API_KEY=...
sops --encrypt > .env.production.enc

# 2. The orchestrator code (src/fastapi/app/agent/orchestrator.py) reads
#    LLM_BACKEND at startup and selects the adapter. Restart FastAPI:
docker compose restart fastapi

# 3. Smoke test:
curl -X POST http://localhost:8000/internal/queries \
    -H "X-Service-Key: $FASTAPI_SERVICE_KEY" \
    -H "Authorization: Bearer $TEST_JWT" \
    -d '{"query":"how many drill holes are in the project?","project_id":"<uuid>"}'
```

If the smoke test fails with auth errors, the JWT secret may have changed
during the swap. See `secret-rotation.md`.

## Memory'd gotchas to remember

From `feedback_datastore_gotchas.md`:

> **Gotcha 4** — Ollama `OLLAMA_NUM_CTX` env is ONLY a server default; the
> chat API reloads models at their built-in context unless `options.num_ctx`
> is in the request body.

Every chat request to Ollama must include `options.num_ctx`. The
orchestrator code at `_call_openai_compatible_llm` already does this
(per Module 6 Chunk 3). After a model swap, verify the per-request
`num_ctx` matches the new model's context (Qwen 3 30B-A3B = 16K, smaller
distills = 4K-8K).

```bash
# Spot-check post-swap:
docker compose exec ollama ollama ps
# CONTEXT column should match what you set, not the model's built-in default.
```

## Cost monitoring after swap

The `LLM_COST_USD` metric (from `src/fastapi/app/metrics.py`) tracks
per-call cost. Watch the rolling 24h panel on the RAG Quality dashboard
for 1-2 days post-swap to confirm cost matches the model-card estimate.

If cost diverges by >20% from prediction, investigate the prompt
construction (maybe context_packing is leaking unnecessary tokens).

## Audit trail

```bash
docker compose exec laravel-octane php artisan tinker
>>> Log::channel('authz_audit')->info('llm_model_swap', [
...     'from' => 'ollama:qwen3:30b-a3b-q4_K_M',
...     'to' => 'vllm:deepseek-v3',
...     'actor' => 'kyle@example.com',
...     'reason' => 'gpu_upgrade_to_l40s',
... ]);
```

## Cross-references

- `docs/model_migration.md` — design rationale for the current model choice.
- `ops/runbooks/secret-rotation.md` — env file rotation tooling.
- `ops/runbooks/deploy-rollback.md` — if the swap breaks something.
- `feedback_datastore_gotchas.md` (memory) — Gotcha 4 (num_ctx sticky).
- `src/fastapi/app/agent/orchestrator.py` — `_call_openai_compatible_llm`
  and the Anthropic adapter.
- `src/fastapi/app/metrics.py` — LLM_COST_USD, LLM_TOKENS_OUTPUT counters.
