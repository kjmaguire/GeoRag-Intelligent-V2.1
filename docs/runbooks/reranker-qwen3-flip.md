# Runbook — flip the live reranker to Qwen3-Reranker-0.6B

**Status (2026-06-29):** VALIDATED CLEAR WIN, deployment **VRAM-gated**. Live
reranker stays **bge-reranker-base** until VRAM is freed (see §3).

## 1. The result (why flip)

Golden-set rerank eval (`scripts/eval_reranker_qwen3_vs_bge.py`, 15 golden
queries, retrieve top-20 → rerank → NDCG@10):

| reranker            | mean NDCG@10 |
|---------------------|--------------|
| dense (no rerank)   | 0.5080 |
| bge-reranker-base   | 0.6188 |
| **Qwen3-Reranker-0.6B** | **0.7048** |

Qwen3 beats bge by **+0.086 NDCG (+13.9%)** — well above the 0.02 "clear win"
bar. NOTE: this is the **stock** Qwen3-Reranker via the causal-LM yes/no-logit
path (`app.services.reranker._Qwen3CausalReranker`). It is NOT the fine-tuned
variant that earned the 3 prior HOLD verdicts — those overfit; stock wins.

## 2. Why it isn't already flipped

The causal-LM reranker MUST run on GPU (on CPU it blows the 8 s rerank timeout —
~20 forward passes/query). It needs ~1.3 GB VRAM. With the full stack up
(vllm 0.72 + vllm-vl + embedding/sparse sidecars) the A4500 has only **~0.6 GB
free** — it does not fit. Forcing it risks OOM-crashing the chat-critical
`vllm`. So this is a resource-allocation decision, not a blind flag flip.

## 3. Free ~1.3 GB VRAM first — pick ONE

- **(a) Drop vllm-vl** (frees ~3.5 GB): stop `georag-vllm-vl`. Cost: no figure/VL
  captioning ingest. Cleanest if VL ingest is not in active use.
- **(b) Trim main vLLM KV-cache**: lower `--gpu-memory-utilization` 0.72 → ~0.66
  (frees ~1.2 GB) and recreate `vllm`. Cost: fewer concurrent sequences / less
  KV headroom for the 14B. Verify chat still serves long contexts.
- **(c) Add a second GPU** (real fix for hosting reranker + VL + LLM together).

## 4. Flip steps (after VRAM is free)

1. Give the reranker sidecar a GPU + the Qwen3 backend. In `docker-compose.yml`
   under the `reranker:` service add `gpus: all` (or a device reservation) and:
   ```yaml
   environment:
     RERANKER_BACKEND: qwen3_causal
     RERANKER_DEVICE: cuda
     QWEN3_RERANKER_MODEL: Qwen/Qwen3-Reranker-0.6B   # already in the HF cache
   ```
2. Recreate ONLY the reranker sidecar:
   ```
   docker compose -p georagintelligencev10 --env-file .env --profile dev-full \
     up -d --no-build --no-deps reranker
   ```
3. Verify: `docker logs georag-reranker` shows "Reranker ready: qwen3-causal:…";
   `nvidia-smi` shows vllm still healthy (no OOM); run a real chat query and
   confirm citations + latency are within budget.
4. Rollback if vllm destabilises: unset the two env vars + recreate → back to
   bge (CPU). The model code (`_Qwen3CausalReranker`) and download stay.

## 5. Re-validate after a corpus change

Re-run `scripts/eval_reranker_qwen3_vs_bge.py` in a GPU container after any
re-embed / corpus refresh to confirm the win holds before flipping.
