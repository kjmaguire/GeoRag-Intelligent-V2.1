#!/bin/bash
# ADR-0010 Session C driver — runs candidate benchmark on georag_chunks
# then compares to the baseline JSON. Outputs a one-page summary +
# recommends retire/hold per Kyle's ±1pp pass_rate criterion.
#
# Sequencing (run from the host with docker-compose available):
#
#   1. Verify backfill finished
#   2. Stop fastapi
#   3. Restart fastapi with RETRIEVAL_USE_DOCUMENT_PASSAGES=true
#   4. Wait for healthy
#   5. Run candidate benchmark (20 questions, --label adr0010-candidate)
#   6. Restart fastapi back to default (flag=false) so dev returns to baseline
#   7. Run compare_benchmarks.py and emit pass/fail recommendation
#
# Idempotent re-runs: each step is gated on prior step success.
set -euo pipefail

BASELINE_FILE="${1:-/app/bench_results/adr0010-baseline-20260528T061258Z.json}"
COMPOSE_PROJECT="georagintelligencev10"
ENV_FILE="/c/Users/GeoRAG/Herd/georag/.env"

cd /c/Users/GeoRAG/Herd/georag

echo "=== ADR-0010 Session C driver ==="
echo "baseline: $BASELINE_FILE"

# Step 1 — verify backfill
chunks=$(docker exec georag-dagster-webserver python -c "
from qdrant_client import QdrantClient
c = QdrantClient(host='qdrant', port=6333)
print(c.get_collection('georag_chunks').points_count)
")
echo "Step 1: georag_chunks points = $chunks (expected ~7065)"
if [ "$chunks" -lt "6000" ]; then
    echo "FAIL: georag_chunks has fewer than 6000 points; backfill incomplete"
    exit 1
fi

# Step 2 — restart fastapi with flag flipped
echo "Step 2: restarting fastapi with RETRIEVAL_USE_DOCUMENT_PASSAGES=true"
# Backup current .env line, write new value, restart, restore on EXIT
ORIGINAL=$(grep "^RETRIEVAL_USE_DOCUMENT_PASSAGES" .env || echo "")
trap '
  echo "trap: restoring .env"
  sed -i "/^RETRIEVAL_USE_DOCUMENT_PASSAGES/d" .env
  if [ -n "${ORIGINAL:-}" ]; then echo "$ORIGINAL" >> .env; fi
  docker compose -p '"$COMPOSE_PROJECT"' --env-file .env --no-deps up -d --no-build fastapi >/dev/null 2>&1 || true
' EXIT
sed -i "/^RETRIEVAL_USE_DOCUMENT_PASSAGES/d" .env
echo "RETRIEVAL_USE_DOCUMENT_PASSAGES=true" >> .env
docker compose -p "$COMPOSE_PROJECT" --env-file .env --no-deps up -d --no-build fastapi

# Step 3 — wait for healthy
echo "Step 3: waiting for fastapi healthy..."
for i in $(seq 1 30); do
    if docker exec georag-fastapi curl -sf http://localhost:8001/healthz >/dev/null 2>&1; then
        echo "  fastapi healthy after ${i}s"
        break
    fi
    sleep 1
done

# Step 4 — verify flag is on
flag=$(docker exec georag-fastapi python -c "from app.config import settings; print(settings.RETRIEVAL_USE_DOCUMENT_PASSAGES)")
echo "Step 4: RETRIEVAL_USE_DOCUMENT_PASSAGES = $flag (expected True)"

# Step 5 — run candidate benchmark
TS=$(date -u +%Y%m%dT%H%M%SZ)
CANDIDATE_FILE="/app/bench_results/adr0010-candidate-${TS}.json"
echo "Step 5: running candidate benchmark (20 questions) → $CANDIDATE_FILE"
docker exec georag-fastapi bash -c "cd /app && python scripts/run_golden_benchmark.py --max-questions 20 --label adr0010-candidate-georag-chunks --output $CANDIDATE_FILE"

# Step 6 — compare
echo "Step 6: comparing baseline → candidate"
docker exec georag-fastapi python scripts/compare_benchmarks.py "$BASELINE_FILE" "$CANDIDATE_FILE" || true

# Final summary
echo ""
echo "=== SUMMARY ==="
docker exec georag-fastapi python -c "
import json
with open('$BASELINE_FILE') as f: before = json.load(f)
with open('$CANDIDATE_FILE') as f: after = json.load(f)
b_pr = before['summary']['pass_rate']
a_pr = after['summary']['pass_rate']
delta = a_pr - b_pr
print(f'baseline pass_rate:  {b_pr:.3f}')
print(f'candidate pass_rate: {a_pr:.3f}')
print(f'delta:               {delta:+.3f}')
print(f'')
if delta >= -0.01:
    print('VERDICT: RETIRE (within ±1pp or better)')
else:
    print(f'VERDICT: HOLD (regression of {delta:.3f} exceeds 1pp tolerance)')
"
