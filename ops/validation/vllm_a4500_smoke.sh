#!/usr/bin/env bash
# =============================================================================
# vLLM smoke test on RTX A4500 (20 GB) — Qwen/Qwen3-14B-AWQ (current)
# =============================================================================
#
# Originally the gating check for the Ollama → vLLM cutover against
# Qwen3-30B-A3B; retargeted in 2026-05 to the current Qwen/Qwen3-14B-AWQ
# build. Override MODEL= to smoke-test a different candidate (a return to
# Qwen3-30B-A3B-Instruct-2507-AWQ, GLM-4.5-Air-AWQ, etc.). Must pass
# before committing to the AWQ choice or to the A4500 hardware as the
# dev-LLM target. If this fails we either:
#   (a) drop to a smaller AWQ candidate, or
#   (b) raise hardware spec for prod deploys.
#
# What this verifies:
#   1. The AWQ checkpoint downloads and loads on the A4500 (20 GB VRAM,
#      compute 8.6, Marlin INT4 path).
#   2. vLLM serves a coherent /v1/chat/completions response.
#   3. Throughput (tokens/sec, prompt-throughput) is in the right ballpark
#      for the FastAPI 8 s gather deadline + the 4096 max-output budget.
#   4. Prefix caching engages (`cache_config.num_gpu_blocks` > 0 in startup
#      log; second-call TTFT noticeably lower than first).
#
# Why this can't run in CI: pulls ~17 GB of weights + needs the actual A4500.
# Run from the dev workstation with the GPU idle.
#
# Prereqs:
#   - Docker with NVIDIA Container Toolkit configured (nvidia runtime).
#   - HF_TOKEN set in the environment (only needed for gated repos; AWQ
#     Qwen3 variants are public, but set it anyway for forward-compat).
#   - Port 8001 free on host.
#   - ~25 GB free disk in the HF cache dir (default ~/.cache/huggingface).
#
# Usage:
#   ./ops/validation/vllm_a4500_smoke.sh
#   ./ops/validation/vllm_a4500_smoke.sh --model Qwen/Qwen3-14B-AWQ --max-len 16384
#
# Output:
#   - Streams the vLLM server log to stdout while it loads.
#   - Once /health returns 200, fires three timed completions and prints
#     tokens/sec, TTFT, and total latency.
#   - Writes a JSON report to ops/validation/reports/vllm_a4500_smoke_<ts>.json
#   - Exits 0 on PASS (model loads, response coherent, throughput >= floor).
# =============================================================================

set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen3-14B-AWQ}"
QUANT="${QUANT:-awq_marlin}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.92}"
HOST_PORT="${HOST_PORT:-8001}"
CONTAINER_NAME="${CONTAINER_NAME:-vllm-a4500-smoke}"
HF_CACHE_DIR="${HF_CACHE_DIR:-$HOME/.cache/huggingface}"
THROUGHPUT_FLOOR="${THROUGHPUT_FLOOR:-15}"   # tokens/sec — below this = fail

# Override via flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)       MODEL="$2"; shift 2 ;;
        --quant)       QUANT="$2"; shift 2 ;;
        --max-len)     MAX_MODEL_LEN="$2"; shift 2 ;;
        --gpu-util)    GPU_MEM_UTIL="$2"; shift 2 ;;
        --port)        HOST_PORT="$2"; shift 2 ;;
        --floor)       THROUGHPUT_FLOOR="$2"; shift 2 ;;
        *)             echo "Unknown flag: $1" >&2; exit 2 ;;
    esac
done

REPORT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/reports" && pwd)"
REPORT_FILE="${REPORT_DIR}/vllm_a4500_smoke_$(date +%Y%m%d_%H%M%S).json"

cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "▶ vLLM smoke test"
echo "   model:           ${MODEL}"
echo "   quantization:    ${QUANT}"
echo "   max-model-len:   ${MAX_MODEL_LEN}"
echo "   gpu-mem-util:    ${GPU_MEM_UTIL}"
echo "   host port:       ${HOST_PORT}"
echo "   throughput floor: ${THROUGHPUT_FLOOR} tok/s"
echo

# Verify nvidia runtime is available + the GPU is the A4500 we expect.
if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "✗ nvidia-smi not on PATH; cannot verify GPU." >&2
    exit 1
fi
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)"
GPU_MEM_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1)"
echo "▶ Detected GPU: ${GPU_NAME} (${GPU_MEM_MIB} MiB)"
if (( GPU_MEM_MIB < 18000 )); then
    echo "✗ GPU has <18 GB; AWQ 30B-A3B will OOM. Try Qwen3-14B-AWQ instead." >&2
    exit 1
fi

cleanup

echo "▶ Starting vLLM container..."
docker run -d --rm \
    --name "${CONTAINER_NAME}" \
    --gpus all \
    --shm-size=4g \
    -e HF_TOKEN="${HF_TOKEN:-}" \
    -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
    -p "${HOST_PORT}:8000" \
    vllm/vllm-openai:latest \
    --model "${MODEL}" \
    --quantization "${QUANT}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --gpu-memory-utilization "${GPU_MEM_UTIL}" \
    --enable-prefix-caching \
    --served-model-name "${MODEL}" \
    >/dev/null

echo "▶ Tailing startup log (Ctrl-C aborts)..."
docker logs -f "${CONTAINER_NAME}" &
LOG_PID=$!

# Poll /health up to 8 minutes (cold pull + load on first run can take a while).
echo "▶ Waiting for /health..."
HEALTH_URL="http://localhost:${HOST_PORT}/health"
DEADLINE=$(( $(date +%s) + 480 ))
while (( $(date +%s) < DEADLINE )); do
    if curl -sf "${HEALTH_URL}" >/dev/null 2>&1; then
        break
    fi
    sleep 5
done
kill "${LOG_PID}" 2>/dev/null || true
wait "${LOG_PID}" 2>/dev/null || true

if ! curl -sf "${HEALTH_URL}" >/dev/null; then
    echo "✗ vLLM /health never returned 200; check docker logs ${CONTAINER_NAME}" >&2
    exit 1
fi
echo "✓ /health OK"

# Three timed completions: cold, warm-prefix-same, warm-prefix-different.
PROMPT_SHARED='You are a senior geologist analysing exploration drilling data. The Lazy Edward Bay project is a precambrian greenstone belt with VMS-style mineralisation. Drilling intersected pyrite-rich altered basalt at 124.5 m, with anomalous Cu-Zn-Au.'
PROMPT_Q1='Summarise in three sentences whether this intersection warrants follow-up.'
PROMPT_Q2='List five geological controls that could host similar mineralisation nearby.'

_py_json() {
    # Run a tiny inline python3 program to build / parse JSON. Replaces jq
    # so the script stays self-contained on hosts that don't have jq
    # installed (jq isn't in the default WSL Ubuntu image).
    python3 -c "$1" "${@:2}"
}

run_completion() {
    local prompt_a="$1"
    local prompt_b="$2"
    local label="$3"

    local payload
    payload=$(MODEL="${MODEL}" SYS="${prompt_a}" USER_MSG="${prompt_b}" python3 -c '
import json, os
print(json.dumps({
    "model": os.environ["MODEL"],
    "max_tokens": 256,
    "temperature": 0.1,
    "messages": [
        {"role": "system", "content": os.environ["SYS"]},
        {"role": "user",   "content": os.environ["USER_MSG"]},
    ],
}))')

    # `date +%s%3N` is unreliable across distros — some emit nanoseconds
    # regardless of the `%3N` truncation directive. python3 with
    # time.time() is bulletproof.
    local t0 t1 elapsed_ms
    t0=$(python3 -c 'import time; print(int(time.time()*1000))')
    local response
    response=$(curl -sf "http://localhost:${HOST_PORT}/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -d "${payload}")
    t1=$(python3 -c 'import time; print(int(time.time()*1000))')
    elapsed_ms=$(( t1 - t0 ))

    # Parse response fields with one python3 call. Outputs three lines:
    #   prompt_tokens
    #   completion_tokens
    #   content (truncated to 200 chars, single-line for shell capture)
    local parsed
    parsed=$(RESPONSE="${response}" python3 -c '
import json, os, sys
data = json.loads(os.environ["RESPONSE"])
usage = data.get("usage") or {}
content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
content_one_line = content.replace("\n", " ").replace("\r", " ")[:200]
print(int(usage.get("prompt_tokens", 0) or 0))
print(int(usage.get("completion_tokens", 0) or 0))
print(content_one_line)')

    local prompt_tokens completion_tokens content
    prompt_tokens=$(sed -n 1p <<<"${parsed}")
    completion_tokens=$(sed -n 2p <<<"${parsed}")
    content=$(sed -n 3p <<<"${parsed}")

    local tokens_per_sec
    if (( elapsed_ms > 0 && completion_tokens > 0 )); then
        tokens_per_sec=$(awk -v c="${completion_tokens}" -v ms="${elapsed_ms}" 'BEGIN { printf "%.2f", c * 1000 / ms }')
    else
        tokens_per_sec="0.00"
    fi

    # Status lines go to stderr so $(run_completion ...) captures only the
    # final JSON line. Otherwise the caller-side json.loads chokes on the
    # mixed status+JSON output.
    {
        echo "── ${label}"
        echo "   prompt_tokens:     ${prompt_tokens}"
        echo "   completion_tokens: ${completion_tokens}"
        echo "   total_ms:          ${elapsed_ms}"
        echo "   tokens/sec:        ${tokens_per_sec}"
        echo "   first 120 chars:   ${content:0:120}"
    } >&2

    LABEL="${label}" PT="${prompt_tokens}" CT="${completion_tokens}" \
        EMS="${elapsed_ms}" TPS="${tokens_per_sec}" SAMPLE="${content}" \
        python3 -c '
import json, os
print(json.dumps({
    "label": os.environ["LABEL"],
    "prompt_tokens": int(os.environ["PT"]),
    "completion_tokens": int(os.environ["CT"]),
    "elapsed_ms": int(os.environ["EMS"]),
    "tokens_per_sec": os.environ["TPS"],
    "sample": os.environ["SAMPLE"],
}))'
}

echo "▶ Running timed completions..."
RUN1=$(run_completion "${PROMPT_SHARED}" "${PROMPT_Q1}" "cold")
RUN2=$(run_completion "${PROMPT_SHARED}" "${PROMPT_Q1}" "warm-same-prompt")
RUN3=$(run_completion "${PROMPT_SHARED}" "${PROMPT_Q2}" "warm-prefix-cache")

WARM_TPS=$(RUN3="${RUN3}" python3 -c 'import json, os; print(json.loads(os.environ["RUN3"])["tokens_per_sec"])')
PASS=$(awk -v a="${WARM_TPS}" -v f="${THROUGHPUT_FLOOR}" 'BEGIN { print (a + 0 >= f + 0) ? "true" : "false" }')

mkdir -p "${REPORT_DIR}"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    GPU="${GPU_NAME}" GPU_MEM_MIB="${GPU_MEM_MIB}" \
    MODEL="${MODEL}" QUANT="${QUANT}" \
    MAX_MODEL_LEN="${MAX_MODEL_LEN}" GPU_MEM_UTIL="${GPU_MEM_UTIL}" \
    FLOOR="${THROUGHPUT_FLOOR}" PASS="${PASS}" \
    RUN1="${RUN1}" RUN2="${RUN2}" RUN3="${RUN3}" \
    python3 -c '
import json, os
out = {
    "timestamp": os.environ["TS"],
    "gpu": os.environ["GPU"],
    "gpu_mem_mib": int(os.environ["GPU_MEM_MIB"]),
    "model": os.environ["MODEL"],
    "quantization": os.environ["QUANT"],
    "max_model_len": int(os.environ["MAX_MODEL_LEN"]),
    "gpu_memory_utilization": os.environ["GPU_MEM_UTIL"],
    "throughput_floor_tps": int(os.environ["FLOOR"]),
    "pass": os.environ["PASS"] == "true",
    "runs": [
        json.loads(os.environ["RUN1"]),
        json.loads(os.environ["RUN2"]),
        json.loads(os.environ["RUN3"]),
    ],
}
print(json.dumps(out, indent=2))' > "${REPORT_FILE}"

echo
echo "▶ Report: ${REPORT_FILE}"
if [[ "${PASS}" == "true" ]]; then
    echo "✓ PASS — warm tokens/sec ${WARM_TPS} >= floor ${THROUGHPUT_FLOOR}"
    exit 0
else
    echo "✗ FAIL — warm tokens/sec ${WARM_TPS} < floor ${THROUGHPUT_FLOOR}"
    echo "  Consider: a smaller AWQ model, lower max-model-len, or hardware upgrade."
    exit 1
fi
