# Capacity Planning — V1

> **⚠️ MODEL UPDATE (2026-05-23):** the model sized in this doc
> (`qwen3:30b-a3b` MoE @ ~18 GB) has been **reverted** to
> `Qwen/Qwen3-14B-AWQ` (dense, ~8 GB). The VRAM math below is preserved
> as the record of why MoE was attractive on a 20 GB A4500, but the
> running configuration is now: vLLM serving Qwen3-14B AWQ at
> `VLLM_GPU_MEM_UTIL=0.80` with the freed VRAM allocated to
> `hatchet-worker-ai` (bge-small + bge-reranker-base + SPLADE++).
> See `docs/model_migration.md` (Current State header) for full
> rationale.

> **Hardware refresh (2026-05-08):** the actual dev workstation is **NOT**
> a 32-core / 128 GB / L40S box as the v1.12 capacity plan claimed —
> that was a planning aspiration. Real dev hardware is:
>
>   * **AMD Ryzen Threadripper Pro 5955WX** — 16 physical cores / 32 threads (Zen 3, 280 W TDP)
>   * **64 GB RAM** (DDR4 ECC, single-tenant)
>   * **NVIDIA RTX A4500** — 20 GB GDDR6 VRAM (Ampere GA102, 7168 CUDA cores, 640 GB/s)
>   * **1.8 TB NVMe** dedicated
>   * **Single-purpose machine** — no other workloads on this box, so we
>     can pin VRAM, schedule aggressively, and skip the "leave room for
>     the user" defaults that single-workstation guides assume.
>
> Implications for tuning:
>
> 1. **VRAM (20 GB)** is sufficient for a single MoE model (qwen3:30b-a3b
>    Q4_K_M ~17.4 GB + 16K KV q8_0 ~400 MB = ~17.8 GB, leaves ~2.2 GB
>    headroom). Q5_K_M (~20.4 GB) is feasible **only with a small CPU
>    offload** (~0.5–1 GB) — viable on this Threadripper because the
>    offloaded layers run fast on Zen 3 with `QWEN3_NUM_THREAD=12`.
> 2. **Tier routing (FAST + DEEP simultaneously) does NOT fit on 20 GB**.
>    qwen3:8b (~5 GB) + qwen3:30b-a3b (~18 GB) = 23 GB exceeds budget.
>    Tier-routing scaffolding stays in code (unit-tested) for staging /
>    prod where ≥24 GB cards are available; on this hardware,
>    `OLLAMA_TIER_ROUTING_ENABLED=false` and every query hits the MoE.
> 3. **CPU is the new headroom**. 16 physical cores / 32 threads is
>    ~2× what the prior plan assumed. We can push `QWEN3_NUM_THREAD=12`
>    on the offload path, FastAPI uvicorn workers to 6, Postgres
>    `max_parallel_workers=12`, and run Dagster ingestion concurrently
>    with serving without contention.
> 4. **Context window can grow**. The 20 GB headroom means
>    `OLLAMA_NUM_CTX=24576` (24K) is safe — KV cache at 24K + q8_0 is
>    ~600 MB, total still ~18.0 GB. Context-budget callers should use
>    `MAX_CONTEXT_TOKENS=22000` to leave system-prompt + completion
>    headroom inside the larger window.
>
> The MoE telemetry sidecar (`docker/compose.moe-telemetry.yml`) and the
> v1.5-26 Q5_K_M validator run remain the right next steps — Q5_K_M with
> a Threadripper-friendly 0.5 GB offload is *exactly* the case the
> validator should now measure on this hardware.

**Module 10 Chunk 10.7** — analytical doc. Pairs with `2026-04-22-api-latency.md`
(per-query latency baseline) to answer "this stack supports N concurrent users
on hardware H."

## Reference hardware

| Class | Spec |
|-------|------|
| **Dev workstation (Kyle's, actual)** | AMD Threadripper Pro 5955WX (16C/32T), 64 GB RAM, NVIDIA RTX A4500 20 GB VRAM (Ampere), 1.8 TB NVMe — **single-purpose, dedicated to GeoRAG** |
| **Staging (planned)** | 16-core CPU, 64 GB RAM, single NVIDIA L4 24 GB VRAM, 2 TB NVMe |
| **Production (small client)** | 32-core CPU, 128 GB RAM, single L40S 48 GB VRAM, 8 TB NVMe RAID-1 |

GeoRAG is **single-node by design** for V1. Scaling to multi-node is V1.5+
work and primarily a SeaweedFS / Qdrant story; the LLM serving will likely
remain single-node-per-tenant for the foreseeable future (model cold-start
cost dwarfs request-routing cost at this scale).

## Bottlenecks ranked by usual-suspect order

| Rank | Bottleneck | Symptom | Mitigation |
|------|------------|---------|------------|
| 1 | LLM serving (Ollama / vLLM) | Queries queue at the LLM regardless of FastAPI capacity | Faster GPU OR smaller model OR batched generation |
| 2 | Horizon worker pool | `horizon_queue_depth` climbs; SSE dispatch lags | `php artisan horizon:supervisor:add` more workers |
| 3 | FastAPI uvicorn workers | `octane_workers_busy / total > 0.85` | Bump `--workers N` in compose |
| 4 | Qdrant search latency | `georag-services` dashboard: Qdrant p95 climbs | HNSW `ef_search` tune OR `m` rebuild |
| 5 | Postgres connection pool (PgBouncer) | `pg_stat_activity_count / max_connections > 0.8` | Bump PgBouncer `default_pool_size` |
| 6 | Redis memory | `redis_memory_used_bytes / max_bytes > 0.85` | Cache TTL tightening OR larger maxmemory |

In practice on the reference dev workstation, items 1 and 2 hit first and
dominate. Items 3-6 are surfaced by the dashboards from Module 10
Chunks 10.4/10.5 and triggered by the alert rules.

## Concurrent-user estimates

These are **first-pass** estimates derived from the architecture's stated
SLAs (§06 timeout values, §05c expected-latency commentary). Validate
against real measurements once the perf baseline lands.

### Reference dev workstation (Threadripper Pro 5955WX 16C/32T, 64 GB, A4500 20 GB)

This is **a developer machine** — its concurrency targets are sized to
exercise the SSE / streaming / multi-tool fan-out paths under realistic
single-tenant load, not to validate prod-tier multi-user numbers.

- **2 sustained concurrent users** at p95 < 6s on 30B-A3B Q4_K_M with
  16K context. The A4500's ~640 GB/s memory bandwidth × ~30B-active-equivalent
  throughput pencils to ~14–18 tok/s warm — single-stream is excellent,
  parallel streams hit the GPU bottleneck quickly.
- **4-6 concurrent users** burst (queries queue at the LLM, p95 climbs
  to ~12s but no errors). The 32 logical CPU threads soak retrieval +
  Postgres + Dagster pressure that dominated the 8-core era.
- **>6 concurrent users** = degraded UX. The dev machine is not a
  load-test rig; if you need to validate >6 concurrent, mirror the
  staging compose to a real GPU node.

Bottlenecks specific to this hardware:

* **VRAM is the cap, not compute.** 20 GB fits Q4_K_M MoE comfortably and
  Q5_K_M with a small offload. Q6_K (~24.5 GB) requires ~5 GB CPU offload
  and is probably not worth the throughput hit on a 16-core box.
* **CPU has headroom.** 16C/32T means we can simultaneously run
  ingestion (Dagster), Postgres bulk operations (`CLUSTER`, `ANALYZE`,
  `REINDEX CONCURRENTLY`), and serve queries — none of those wedged the
  serving path on the 8-core era. Don't undersize uvicorn / Octane
  workers; the cores are there.
* **Memory is balanced.** 64 GB is enough for the 8 GB Postgres
  shared_buffers + 32 GB effective_cache_size + 20 GB OS / containers /
  cache layers. Add a swap warning if Dagster ingestion grows large
  Polars frames — those are out-of-VRAM and live in host RAM.
* **NVMe handles WAL + ingestion side-by-side.** 1.8 TB is plenty for
  ~1 year of dev corpus growth + WAL archive on a single device.

### Staging (16c / 64 GB / L4)

- **2 sustained concurrent users** at p95 < 5s.
- **4-6 concurrent users** burst.
- **>6 concurrent users** = degraded.

L4's 24 GB VRAM caps the model size we can serve. Qwen3-30B-A3B AWQ
INT4 (~17 GB weights) fits with ~6 GB KV-cache headroom — enough for
`max-model-len ≈ 24K` at the staging concurrency level above. Larger
models (BF16 30B at ~60 GB; 70B-class anything) would need an upgrade
to L40S 48 GB or A100 80 GB.

### Production (32c / 128 GB / L40S, RAID-1 NVMe)

- **6-8 sustained concurrent users** at p95 < 5s (assuming shared_buffers
  tuning per `ops/runbooks/datastore-tuning.md`).
- **15-20 concurrent users** burst.

The per-tenant capacity is similar to dev because the LLM is the bottleneck.
Multi-tenant scaling is via separate VMs per tenant (V1's preferred shape
for compliance reasons anyway), not horizontal Octane.

## Workload mix assumptions

| Query class | Frequency | LLM cost | Retrieval cost |
|-------------|-----------|----------|----------------|
| count | 30% | low (1 tool call, no synth) | postgres only |
| exists | 15% | low (1 tool call) | postgres only |
| numeric | 15% | medium (1-2 tool calls + synth) | postgres + qdrant |
| spatial | 10% | medium | postgres-PostGIS + qdrant |
| document | 15% | high (full synth, long context) | qdrant-heavy |
| graph | 10% | medium-high | neo4j + qdrant |
| refusal | 5% | low (cheap synth or no synth) | retrieval may run + fail |

Document-class queries are 3-5× more expensive than count-class and
dominate p99. Spatial-class is the lowest-variance because the CRS
math is deterministic and the result-set size is bounded.

## When to scale

| Signal | Action |
|--------|--------|
| Sustained p95 above latency baseline by 20% on the perf-baseline CI job | Investigate per the table above |
| `horizon_queue_depth > 1000` for 5+ min | Add Horizon supervisors |
| `georag_query_total` rate doubled vs baseline | Likely a real growth signal — prep for tier upgrade |
| `pg_stat_activity_count > 80% of max_connections` | Bump PgBouncer pool OR reduce per-handler hold time |
| GPU utilisation > 90% sustained | Plan for second GPU OR swap to smaller distill OR add tail-sampling on retrieval cache |

## Ollama serving ceiling (per-GPU, dev primary)

The dev primary `qwen3:30b-a3b` runs on Ollama with `OLLAMA_NUM_PARALLEL=1`
on a 16 GB-class GPU (RTX 4080). This is the **actual concurrent-LLM-call
ceiling per GPU**, not just a tuning knob:

- `OLLAMA_NUM_PARALLEL=1` → one in-flight chat completion at a time. The
  next request is held in `OLLAMA_MAX_QUEUE` (32) and starts after the
  current one finishes.
- Concurrent users beyond 1 in-flight LLM call queue against the GPU.
  When the queue is full the orchestrator's HTTP client returns 503 and
  FastAPI surfaces a fail-fast error — the goal is "degrade visibly"
  rather than "blow the FastAPI 8 s budget on a 30 s queue wait."
- Raising `OLLAMA_NUM_PARALLEL` to 2 requires either dropping the model
  to a smaller quant OR moving to a 24 GB+ GPU (RTX 4090 / L4 / A5000)
  so the per-session KV-cache reservation fits. Math at q8_0 KV /
  16K context: ~400 MB × 2 sessions = 800 MB additional VRAM beyond
  the 18 GB resident weights, which a 16 GB card cannot afford.

Practical takeaway for the concurrent-user estimates above: the "burst"
numbers assume short LLM calls (<3 s) so the queue drains before
user-perceived latency degrades. Document-class queries (longer
synthesis, ~6-8 s on the dev box) reduce burst capacity proportionally.

## KV-cache quantisation — pending MoE re-validation

`OLLAMA_KV_CACHE_TYPE=q8_0` was sized against the previous primary
`qwen2.5:14b` (dense). MoE attention patterns differ; the q8_0
"near-zero quality drop" claim has not been re-measured under
`qwen3:30b-a3b`. Re-validation is a V1.5 follow-up — add a short golden-query
quality run with `OLLAMA_KV_CACHE_TYPE` swept across `f16`, `q8_0`,
`q4_0` before relying on the q8_0 budget headroom for production
capacity planning. Until then, treat the 16 GB / 16K-ctx headroom
numbers as MoE-untested.

## Soak / endurance test

V1 does NOT include a soak test. A 24-hour soak at sustained load is on
the V1.5 backlog (`ops/backlog/`). Operationally, the alert rules from
Chunk 10.4 catch slow leaks via `pulse_exception_total` rate + Redis
memory growth.

## Change log

- **2026-04-22 (Chunk 10.7)**: file created with first-pass capacity model.
  Numbers are estimates. Validate against measured perf baselines once
  the nightly job has 7+ days of history.
- **2026-04-27 (Qwen review)**: added Ollama serving ceiling section
  (`OLLAMA_NUM_PARALLEL=1` is a real per-GPU ceiling, not a tuning knob)
  and flagged KV-cache q8_0 as MoE-untested pending V1.5 re-validation.
