#!/usr/bin/env bash
# Qwen3 KV-cache quantisation sweep — V1.5-24.
#
# Compares OLLAMA_KV_CACHE_TYPE in {f16, q8_0, q4_0} against the golden
# rubric on `qwen3:30b-a3b`. The "near-zero quality drop at q8_0" claim
# was measured on dense models — MoE attention may behave differently,
# and our VRAM headroom math assumes q8_0 is safe.
#
# This script orchestrates the OUTSIDE: it edits the ollama service env
# var, restarts the ollama container, warms the model, then invokes the
# in-container validator with the new config. Each run produces a
# JSON report under ops/validation/reports/kv_sweep_<config>_<ts>.json.
#
# After all three configs run, the operator compares scores. The decision
# rule is:
#   - q8_0 within 2% of f16 score → keep q8_0 (current default)
#   - q8_0 regresses >2%          → fall back to f16, document VRAM budget
#                                    impact in capacity-planning.md
#   - q4_0 within 5% of q8_0      → consider q4_0 for capacity scaling
#                                    (frees ~200 MB additional headroom)
#
# Why a shell script and not a Python sweep: changing
# OLLAMA_KV_CACHE_TYPE requires reloading the model with new options —
# the cleanest reload mechanism is a container restart, which has to
# happen on the host. The Python validator handles the actual scoring.
#
# Prerequisites:
#   - dev-llm profile up (ollama + georag-fastapi + the Qwen3 model pulled)
#   - GPU passthrough working (validator queries pynvml/nvidia-smi)
#
# Usage:
#   ./ops/validation/qwen3_kv_cache_sweep.sh
#
# Roughly 10-15 minutes total (warm-up + 5 prompts × 3 configs).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

CONFIGS=("f16" "q8_0" "q4_0")
MODEL="${QWEN3_VALIDATION_MODEL:-qwen3:30b-a3b}"
TS="$(date +%s)"

# We mutate compose env via a one-shot override file rather than touching
# the committed docker-compose.yml. Restored automatically at exit.
OVERRIDE="docker-compose.kv-sweep.override.yml"
trap 'rm -f "$OVERRIDE"' EXIT

run_config() {
    local kv_type="$1"
    echo "==========================================================="
    echo "  KV cache type: $kv_type"
    echo "==========================================================="

    cat > "$OVERRIDE" <<YAML
services:
  ollama:
    environment:
      OLLAMA_KV_CACHE_TYPE: "$kv_type"
YAML

    # Restart ollama with the override. -f order matters: base then override.
    docker compose -f docker-compose.yml -f "$OVERRIDE" \
        --profile dev-llm up -d --force-recreate ollama

    echo "Waiting for ollama to be healthy…"
    for _ in $(seq 1 30); do
        if docker exec georag-ollama ollama list >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done

    # Warm the model with explicit num_ctx — without this it loads at
    # the model's built-in 4K default regardless of OLLAMA_NUM_CTX env
    # (Module 5 memory gotcha #1).
    echo "Warming model $MODEL with num_ctx=16384…"
    docker exec georag-ollama bash -c "
        curl -fsS http://localhost:11434/api/generate \
            -d '{\"model\":\"$MODEL\",\"prompt\":\"warmup\",\"stream\":false,\"options\":{\"num_ctx\":16384}}' \
            >/dev/null
    "

    # Invoke the existing MoE validator scoped to a single model. It
    # writes its own JSON report under ops/validation/reports/.
    echo "Running validator…"
    docker exec \
        -e VALIDATION_OUTPUT_DIR="ops/validation/reports/kv_sweep_${kv_type}_${TS}" \
        -e BASELINE_MODEL="$MODEL" \
        -e TEST_MODELS="$MODEL" \
        georag-fastapi \
        python /app/ops/validation/qwen_moe_validator.py
}

mkdir -p ops/validation/reports

for kv in "${CONFIGS[@]}"; do
    run_config "$kv"
done

echo
echo "==========================================================="
echo "All three configs complete. Report dirs:"
ls -d "ops/validation/reports/kv_sweep_"*"_${TS}" 2>/dev/null || true
echo
echo "Compare avg_score across configs and update:"
echo "  - docker-compose.yml ollama service KV-cache comment block"
echo "  - ops/baselines/capacity-planning.md 'KV-cache quantisation' section"
echo "  - ops/backlog/v1.5-followups.md (mark v1.5-24 closed)"
echo "==========================================================="
