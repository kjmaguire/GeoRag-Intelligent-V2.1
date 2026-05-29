#!/usr/bin/env bash
# =============================================================================
# scripts/phase19_step2_verify.sh
#
# Phase 19 Step 2 — Neo4j entity seed.
#
#   1. Cypher seed file present + non-trivial
#   2. :Project node exists with the test project_id
#   3. :Deposit {name:'Triple R'} exists, deposit_type contains 'unconformity'
#   4. :QualifiedPerson {name:'Sarah Thompson'} exists with project_id
#   5. :Formation {name:'CGL'} exists with project_id
#   6. :Formation {name:'GPT'} exists with project_id
#   7. (Project)-[:HOSTS]->(Deposit Triple R) edge exists
#   8. (Report)-[:AUTHORED_BY]->(QP Sarah Thompson) edge exists
#   9. Cypher is idempotent (re-run yields same Deposit count)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=9
REPO="${REPO:-/home/georag/projects/georag}"
CYPHER="$REPO/database/raw/phase19/20-neo4j-entities.cypher"
PROJ='019d74a1-fba8-7165-9ae6-a5bf93eef97d'
NEO=georag-neo4j
NEO_USER='neo4j'
NEO_PWD='24kNKWLbX20bgHEXAuMSGjCp228LIfUE'

cy() {
    echo "$1" | docker exec -i "$NEO" cypher-shell -u "$NEO_USER" -p "$NEO_PWD" \
        --format plain 2>/dev/null | tail -n +2 | head -1 | tr -d '"'
}

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
PHASE 19 STEP 2 — Neo4j ENTITY SEED
============================================================
BANNER

# 1) Cypher file present
if [ -s "$CYPHER" ]; then
    lines=$(wc -l < "$CYPHER")
    [ "$lines" -ge 80 ] \
        && check "Cypher seed present ($lines lines)" ok \
        || check "cypher length" fail "only $lines lines"
else
    check "cypher exists" fail "missing"
fi

# 2) Project
n=$(cy "MATCH (p:Project {project_id: '$PROJ'}) RETURN count(p);")
if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
    check "(:Project) for test project_id present" ok
else
    check "project node" fail "count=$n"
fi

# 3) Deposit Triple R + deposit_type
dt=$(cy "MATCH (d:Deposit {name:'Triple R'}) RETURN d.deposit_type;")
case "$dt" in
    *unconformity*) check "(:Deposit Triple R) deposit_type contains 'unconformity' (got '$dt')" ok ;;
    *) check "deposit_type" fail "got '$dt'" ;;
esac

# 4) Sarah Thompson QP
n=$(cy "MATCH (q:QualifiedPerson {name:'Sarah Thompson', project_id:'$PROJ'}) RETURN count(q);")
if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
    check "(:QualifiedPerson Sarah Thompson) present with project_id" ok
else
    check "qp node" fail "count=$n"
fi

# 5) Formation CGL
n=$(cy "MATCH (f:Formation {name:'CGL', project_id:'$PROJ'}) RETURN count(f);")
if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
    check "(:Formation CGL) present with project_id" ok
else
    check "CGL" fail "count=$n"
fi

# 6) Formation GPT
n=$(cy "MATCH (f:Formation {name:'GPT', project_id:'$PROJ'}) RETURN count(f);")
if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
    check "(:Formation GPT) present with project_id" ok
else
    check "GPT" fail "count=$n"
fi

# 7) Project HOSTS Deposit
n=$(cy "MATCH (p:Project {project_id:'$PROJ'})-[:HOSTS]->(d:Deposit {name:'Triple R'}) RETURN count(*);")
if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
    check "(Project)-[:HOSTS]->(Deposit Triple R) edge present" ok
else
    check "HOSTS edge" fail "count=$n"
fi

# 8) Report AUTHORED_BY QP
n=$(cy "MATCH (r:Report)-[:AUTHORED_BY]->(q:QualifiedPerson {name:'Sarah Thompson'}) RETURN count(*);")
if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
    check "(Report)-[:AUTHORED_BY]->(QP Sarah Thompson) edge present" ok
else
    check "AUTHORED_BY edge" fail "count=$n"
fi

# 9) Idempotent re-apply
before=$(cy "MATCH (d:Deposit {project_id:'$PROJ'}) RETURN count(d);")
docker exec -i "$NEO" cypher-shell -u "$NEO_USER" -p "$NEO_PWD" --format plain \
    < "$CYPHER" >/dev/null 2>&1 || true
after=$(cy "MATCH (d:Deposit {project_id:'$PROJ'}) RETURN count(d);")
if [ "$before" = "$after" ]; then
    check "Idempotent re-apply (Deposit count stays $after)" ok
else
    check "idempotency" fail "before=$before after=$after"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
