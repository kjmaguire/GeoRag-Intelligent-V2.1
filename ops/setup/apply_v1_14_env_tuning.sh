#!/usr/bin/env bash
# Surgically apply v1.14 hardware-tuning .env values WITHOUT clobbering
# secrets that already exist in the live WSL .env. Idempotent.
#
# Strategy: for each key, sed-replace the existing line if present, OR
# append it at the end if missing. Never touches lines we don't recognise.
set -euo pipefail

ENV=${1:-/home/georag/projects/georag/.env}

declare -A V=(
  [POSTGRES_SHARED_BUFFERS]=8GB
  [POSTGRES_EFFECTIVE_CACHE_SIZE]=32GB
  [POSTGRES_WORK_MEM]=192MB
  [POSTGRES_MAINTENANCE_WORK_MEM]=2GB
  [POSTGRES_RANDOM_PAGE_COST]=1.1
  [POSTGRES_EFFECTIVE_IO_CONCURRENCY]=256
  [POSTGRES_MAX_WORKER_PROCESSES]=24
  [POSTGRES_MAX_PARALLEL_WORKERS]=12
  [POSTGRES_MAX_PARALLEL_WORKERS_PER_GATHER]=6
  [POSTGRES_MAX_PARALLEL_MAINTENANCE_WORKERS]=6
  [REDIS_MAXMEMORY]=1gb
  [OLLAMA_KEEP_ALIVE]=-1
  [OLLAMA_NUM_CTX]=24576
  [QWEN3_TOP_P]=0.8
  [QWEN3_TOP_K]=20
  [QWEN3_MIN_P]=0.0
  [QWEN3_PRESENCE_PENALTY_NO_THINK]=1.5
  [QWEN3_PRESENCE_PENALTY_STRUCTURED]=0.0
  [QWEN3_NUM_THREAD]=12
  [OLLAMA_TIER_ROUTING_ENABLED]=false
  [OLLAMA_TIER_FAST]=qwen3:8b
  [OLLAMA_TIER_STANDARD]=qwen3:14b
  [OLLAMA_TIER_DEEP]=qwen3:30b-a3b
  [MAX_CONTEXT_TOKENS]=22000
  [MAX_CONTEXT_COLLARS]=30
  [MAX_CONTEXT_DOC_CHUNKS]=8
  [MAX_CONTEXT_GRAPH_ENTITIES]=30
  [MAX_CONTEXT_PG_RECORDS]=20
  [UVICORN_WORKERS]=6
)

CHANGED=0
APPENDED=()

for K in "${!V[@]}"; do
  NEW_VAL=${V[$K]}
  # Match the key at start of a line, capturing the existing assignment.
  if grep -qE "^${K}=" "$ENV"; then
    OLD_VAL=$(grep -E "^${K}=" "$ENV" | head -1 | cut -d= -f2-)
    if [ "$OLD_VAL" != "$NEW_VAL" ]; then
      # Use a sentinel char (|) for sed because some values have / in them
      sed -i "s|^${K}=.*|${K}=${NEW_VAL}|" "$ENV"
      echo "  CHANGED  ${K}: ${OLD_VAL} -> ${NEW_VAL}"
      CHANGED=$((CHANGED+1))
    fi
  else
    echo "${K}=${NEW_VAL}" >> "$ENV"
    APPENDED+=("${K}")
  fi
done

if [ ${#APPENDED[@]} -gt 0 ]; then
  echo
  echo "  APPENDED ${#APPENDED[@]} new key(s): ${APPENDED[*]}"
fi
echo
echo "Total changed/appended: $((CHANGED + ${#APPENDED[@]}))"
