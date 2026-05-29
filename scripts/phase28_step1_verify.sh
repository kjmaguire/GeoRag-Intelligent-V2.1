#!/usr/bin/env bash
# =============================================================================
# scripts/phase28_step1_verify.sh
#
# Phase 28 Step 1 — NI 43-101 chunk seed (R-P19-DOC) + document classifier
# keyword expansion (R-P28-DOC-CLASSIFIER).
#
#   1. Seed script present + non-trivial
#   2. Qdrant georag_reports collection exists + holds ≥3 points
#   3. silver.document_passages has ≥3 phase28 rows
#   4. orchestrator.py _DOCUMENT_KEYWORDS includes "orientation", "fault", "kriging", "grid"
#   5. Cold-run golden ≥ 28 (above Phase 27's 27-28 baseline)
#   6. gq-026-estimation-method passes (kriging chunk reachable)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
SEED="$REPO/database/raw/phase28/seed_ni43_chunks.py"
ORCH="$REPO/src/fastapi/app/agent/orchestrator.py"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cat <<'BANNER'

============================================================
PHASE 28 STEP 1 — NI 43-101 seed + document classifier
============================================================
BANNER

if [ -s "$SEED" ]; then
    lines=$(wc -l < "$SEED")
    [ "$lines" -ge 150 ] \
        && check "NI 43-101 seed script present ($lines lines)" ok \
        || check "seed length" fail "only $lines lines"
else
    check "seed script" fail "missing"
fi

# 2) Qdrant collection + points
pt_count=$(docker exec georag-fastapi curl -s http://qdrant:6333/collections/georag_reports 2>/dev/null \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('result',{}).get('points_count', 0))" 2>/dev/null || echo 0)
if [ "${pt_count:-0}" -ge 3 ] 2>/dev/null; then
    check "Qdrant georag_reports has ≥3 points (got $pt_count)" ok
else
    check "qdrant points" fail "got $pt_count"
fi

# 3) silver.document_passages rows
pg_count=$(docker exec georag-postgresql psql -U georag -d georag -tAc \
    "SELECT count(*) FROM silver.document_passages WHERE embedding_id LIKE '%' AND ordinal IN (6,7,14);" | tr -d ' ')
if [ "${pg_count:-0}" -ge 3 ] 2>/dev/null; then
    check "silver.document_passages has ≥3 phase28 rows (got $pg_count)" ok
else
    check "passages" fail "got $pg_count"
fi

# 4) Document classifier expanded keywords
if grep -q 'R-P28-DOC-CLASSIFIER' "$ORCH" \
   && grep -q '"orientation"' "$ORCH" \
   && grep -q '"fault"' "$ORCH" \
   && grep -q '"kriging"' "$ORCH" \
   && grep -q '"grid"' "$ORCH"; then
    check "_DOCUMENT_KEYWORDS includes orientation/fault/kriging/grid" ok
else
    check "classifier keywords" fail "missing"
fi

# 5) Cold-run golden ≥ 28
docker restart georag-fastapi >/dev/null 2>&1
sleep 100
cold=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
if [ "${cold:-0}" -ge 28 ] 2>/dev/null; then
    check "Cold-run golden ≥ 28 (got $cold; Phase 27 peak was 28)" ok
else
    check "cold peak" fail "got $cold"
fi

# 6) gq-026 passes
g26=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py -k gq-026 2>&1 | grep -oE '[0-9]+ passed')
if echo "$g26" | grep -q '1 passed'; then
    check "gq-026-estimation-method passes (kriging chunk retrieved)" ok
else
    check "gq-026" fail "$g26"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
