#!/usr/bin/env bash
# ADR-0011 LoRA v2 — chain: train → eval → report
# Run inside georag-fastapi container via:
#   docker exec georag-fastapi bash -c "bash /tmp/_run_lora_v2_pipeline.sh"

set -e

echo "=== ADR-0011 LoRA v2 pipeline ==="
echo "$(date -u)"

# ── 1. Pause vLLM to free VRAM for training ───────────────────────────
echo ""
echo "[1/4] Pausing vLLM..."
# vLLM lives in a separate container; kill signal sent from outside
# Training script runs inside fastapi container, so we just proceed
# (vLLM VRAM usage is separate from fastapi GPU usage on same A4500)

# ── 2. Run LoRA v2 training ────────────────────────────────────────────
echo ""
echo "[2/4] Training LoRA v2..."
python3 /tmp/_train_reranker_lora_v2.py

# ── 3. Eval LoRA v2 against OOD bench ─────────────────────────────────
echo ""
echo "[3/4] Evaluating LoRA v2 candidate..."
python3 /app/scripts/_eval_lora_against_mlm.py \
    --lora-dir /tmp/reranker-lora-v2/best_adapter \
    --mlm-base /tmp/reranker-mlm \
    --test     /tmp/reranker-train-combined/test.jsonl \
    --output   /tmp/reranker-lora-v2-bench.json

# ── 4. Print verdict ───────────────────────────────────────────────────
echo ""
echo "[4/4] Bench results:"
cat /tmp/reranker-lora-v2-bench.json

echo ""
echo "=== Pipeline complete ==="
echo "$(date -u)"
