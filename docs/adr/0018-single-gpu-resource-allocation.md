# ADR-0018 — Single-GPU resource allocation (reranker > VL ingest)

- **Status:** Accepted (2026-06-29)
- **Deciders:** Kyle (SME), with the 2026-06-29 review
- **Supersedes/relates:** ADR-0015 (Qwen3-VL serving), [[project-vl-serving]], reranker-qwen3-flip runbook

## Context

The dev workstation has ONE RTX A4500 (20 GB). Four GPU tenants compete:

- **main vLLM** — `Qwen/Qwen3-14B-AWQ`, `--gpu-memory-utilization 0.72`, 8K context
  (deliberately 8K for this GPU — see `.env` VLLM_MAX_MODEL_LEN; the compose
  default is 16384, only appropriate on a larger card).
- **vllm-vl** — `Qwen3-VL-4B` for §04p figure captioning (`FIGURE_VL_DESCRIPTIONS`,
  **off by default**).
- **GPU reranker** — `Qwen3-Reranker-0.6B`, +13.9% NDCG@10 over bge, serves
  **every** chat query (~300 ms/20-cand).
- embedding / sparse sidecars — CPU (moved off-GPU 2026-06-24).

Measured (reserved footprints, not bare weights): vLLM ≈ 14.4 GB (0.72 × 20 GB,
weights + KV cache) + vllm-vl ≈ 4.0 GB (4B-8bit, enforce-eager, util-capped
just above weights) + reranker ≈ 1.4 GB → ≈ 19.8 GB, 99 % of 20 GB (181 MiB
free) — too thin; a CUDA-graph/fragmentation spike risks OOM-ing the
chat-critical vLLM. All three cannot coexist with safe headroom on one A4500.

## Decision

**On the single A4500, the GPU reranker takes priority over figure VL ingest.**

- vllm-vl moved off the default `gpu-llm` profile to an **opt-in `vl-ingest`
  profile** (compose). It no longer auto-starts, so the reranker keeps ~8 GB
  free headroom. Chat quality (reranker on every query) beats an off-by-default
  ingest feature.
- **8K context stays** (not 16K) — deliberate for this GPU; raising it costs KV
  cache the reranker now occupies.
- `--reasoning-parser qwen3` is **NOT** enabled — the app manages `<think>` via
  per-call `enable_thinking=False` and never reads `reasoning_content`; enabling
  the parser would move reasoning to an unconsumed field and degrade answers.

## Consequences

- Figure VL captioning requires `--profile gpu-llm --profile vl-ingest` +
  `FIGURE_VL_DESCRIPTIONS=true`, and then either accepting tight VRAM or trimming
  vLLM `--gpu-memory-utilization` (0.72→~0.62). See reranker-qwen3-flip runbook.
- **The durable fix for "all three at once + 16K context" is a second GPU.** Until
  then this is the considered dev allocation, not an oversight.
- Re-evaluate if the workload shifts (e.g., figure ingest becomes routine, or a
  larger card lands).
