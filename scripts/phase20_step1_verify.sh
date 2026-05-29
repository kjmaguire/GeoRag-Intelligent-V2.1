#!/usr/bin/env bash
# =============================================================================
# scripts/phase20_step1_verify.sh
#
# Phase 20 Step 1 — traverse_knowledge_graph SELF-row patch.
#
#   1. tools.py contains the new is_self UNION branch
#   2. tools.py cypher still emits exact + CONTAINS matching branches
#   3. orchestrator.py renders SELF rows with the matched-entity marker
#   4. Live Neo4j traverse returns ≥1 SELF row for 'Triple R'
#   5. The SELF row for Triple R includes deposit_type containing 'unconformity'
#   6. (Sanity) tools.py imports unchanged — no accidental side-effect imports
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
TOOLS="$REPO/src/fastapi/app/agent/tools.py"
ORCH="$REPO/src/fastapi/app/agent/orchestrator.py"
NEO=georag-neo4j
NEO_PWD='24kNKWLbX20bgHEXAuMSGjCp228LIfUE'

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
PHASE 20 STEP 1 — traverse_knowledge_graph SELF-row patch
============================================================
BANNER

# 1) is_self pattern in tools.py
if grep -q 'true AS is_self' "$TOOLS" && grep -q 'NULL AS rel, true AS is_self' "$TOOLS"; then
    check "tools.py contains is_self UNION branch" ok
else
    check "is_self branch" fail "missing"
fi

# 2) exact + CONTAINS branches still present
if grep -q 'toLower(start.name) = toLower' "$TOOLS" \
   && grep -q 'toLower(start.name) CONTAINS toLower' "$TOOLS"; then
    check "tools.py cypher preserves exact + CONTAINS branches" ok
else
    check "match branches" fail "missing"
fi

# 3) SELF render in orchestrator.py
if grep -q "matched entity" "$ORCH"; then
    check "orchestrator.py renders SELF with 'matched entity' marker" ok
else
    check "SELF rendering" fail "marker missing"
fi

# 4) Live SELF row for Triple R
self_count=$(echo "
CALL {
  MATCH (start) WHERE start.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
    AND toLower(start.name) = toLower('triple r')
  MATCH (start)-[r]-(related)
  RETURN start AS source, related AS node, r AS rel, false AS is_self
  UNION
  MATCH (start) WHERE start.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
    AND toLower(start.name) CONTAINS toLower('triple r')
  MATCH (start)-[r]-(related)
  RETURN start AS source, related AS node, r AS rel, false AS is_self
  UNION
  MATCH (start) WHERE start.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
    AND toLower(start.name) CONTAINS toLower('triple r')
  RETURN start AS source, start AS node, NULL AS rel, true AS is_self
}
WITH CASE WHEN is_self THEN 'SELF' ELSE 'EDGE' END AS dir
WITH dir, count(*) AS n WHERE dir = 'SELF'
RETURN n;
" | docker exec -i "$NEO" cypher-shell -u neo4j -p "$NEO_PWD" --format plain 2>/dev/null | tail -1 | tr -d ' ')
if [ "${self_count:-0}" -ge 1 ] 2>/dev/null; then
    check "Live Neo4j returns ≥1 SELF row for 'Triple R' (got $self_count)" ok
else
    check "SELF rowcount" fail "got $self_count"
fi

# 5) Triple R SELF row has deposit_type containing 'unconformity'
dt=$(echo "
MATCH (d:Deposit {name:'Triple R'})
RETURN d.deposit_type;
" | docker exec -i "$NEO" cypher-shell -u neo4j -p "$NEO_PWD" --format plain 2>/dev/null | tail -1 | tr -d '"')
case "$dt" in
    *unconformity*) check "Triple R Deposit.deposit_type contains 'unconformity' (got '$dt')" ok ;;
    *) check "deposit_type" fail "got '$dt'" ;;
esac

# 6) No new top-level imports added (sanity guard)
import_count=$(grep -cE '^(import |from )' "$TOOLS")
if [ "${import_count:-0}" -ge 10 ] 2>/dev/null && [ "${import_count:-0}" -le 60 ] 2>/dev/null; then
    check "tools.py imports unchanged ($import_count import lines, in normal range)" ok
else
    check "imports" fail "import count $import_count out of expected range"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
