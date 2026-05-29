#!/bin/bash
# Overnight chain: wait for Phase 2 MLM artifacts → run Phase 3 full FT → run Phase 4 eval.
# Runs inside georag-fastapi.

set -uo pipefail
exec 2>&1

PHASE2_OUT="/tmp/reranker-mlm"
PHASE3_OUT="/tmp/reranker-ft"
DATASET="/tmp/reranker-train-combined"
TEST_SPLIT="${DATASET}/test.jsonl"
BENCH_OUT="/tmp/reranker-bench.json"

echo "[$(date -u +%FT%TZ)] waiting for Phase 2 MLM checkpoint at $PHASE2_OUT/config.json ..."
until [ -f "$PHASE2_OUT/config.json" ] && [ -f "$PHASE2_OUT/tokenizer.json" ]; do
    sleep 60
done
echo "[$(date -u +%FT%TZ)] Phase 2 artifacts present, kicking off Phase 3 full FT"

# Phase 3
python /app/scripts/_train_reranker_full.py \
    --backbone "$PHASE2_OUT" \
    --dataset-prefix "$DATASET" \
    --output "$PHASE3_OUT" \
    --epochs 3 \
    --batch-size 16 \
    --learning-rate 2e-5 \
    --warmup-ratio 0.1 \
    --max-seq-length 512
PHASE3_EXIT=$?
echo "[$(date -u +%FT%TZ)] Phase 3 exit=$PHASE3_EXIT"

if [ $PHASE3_EXIT -ne 0 ]; then
    echo "[$(date -u +%FT%TZ)] FAILED Phase 3 — skipping Phase 4"
    exit $PHASE3_EXIT
fi

# Phase 4 — bench candidate vs stock baseline
echo "[$(date -u +%FT%TZ)] kicking off Phase 4 NDCG eval"
python /app/scripts/eval_reranker_lora.py \
    --candidate-checkpoint "$PHASE3_OUT" \
    --baseline BAAI/bge-reranker-base \
    --test "$TEST_SPLIT" \
    --output "$BENCH_OUT"
PHASE4_EXIT=$?
echo "[$(date -u +%FT%TZ)] Phase 4 exit=$PHASE4_EXIT"

if [ $PHASE4_EXIT -eq 0 ]; then
    echo "[$(date -u +%FT%TZ)] BENCH RESULTS:"
    cat "$BENCH_OUT"
fi

echo "[$(date -u +%FT%TZ)] OVERNIGHT_CHAIN_DONE"
exit $PHASE4_EXIT
