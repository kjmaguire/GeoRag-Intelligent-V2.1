#!/usr/bin/env bash
# =============================================================================
# scripts/phase12_step3_verify.sh
#
# Phase 12 Step 3 done-definition — Layer 6 constraint externalisation
# (R-P11-l6-config).
#
#   1. layer6_constraints.json exists alongside the layer module
#   2. JSON has 7 constraint entries (matches pre-migration count)
#   3. layer6_constraints.py no longer contains the inline
#      GeologicalConstraint(...) literal list
#   4. layer6_constraints.py loads via _load_constraints_from_json()
#   5. In-container: GEOLOGICAL_CONSTRAINTS still has 7 entries
#      with correct names + bounds (round-trip match)
#   6. End-to-end: an obviously-out-of-bounds value still triggers
#      a violation through _find_violations
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
JSON_FILE="$REPO/src/fastapi/app/agent/hallucination/layer6_constraints.json"
PY_FILE="$REPO/src/fastapi/app/agent/hallucination/layer6_constraints.py"
LARAVEL_FA="${FASTAPI_CONTAINER:-georag-fastapi}"

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
PHASE 12 STEP 3 — LAYER 6 CONSTRAINT EXTERNALISATION
============================================================
BANNER

# 1) JSON file present
if [ -s "$JSON_FILE" ]; then
    check "layer6_constraints.json present" ok
else
    check "json file" fail "missing or empty"
fi

# 2) Seven constraints in JSON
json_count=$(python3 -c "
import json
with open('$JSON_FILE') as f:
    d = json.load(f)
print(len(d.get('constraints', [])))
" 2>/dev/null)
[ "$json_count" = "7" ] \
    && check "JSON contains 7 constraint entries" ok \
    || check "json count" fail "got $json_count / 7"

# 3) Inline literal list removed
if grep -qE 'GEOLOGICAL_CONSTRAINTS: list\[GeologicalConstraint\] = \[\s*$' "$PY_FILE" \
    && grep -q 'GeologicalConstraint($' "$PY_FILE"; then
    check "inline literal list" fail "still present"
else
    check "Inline GeologicalConstraint(...) list literal removed" ok
fi

# 4) Module uses _load_constraints_from_json
if grep -q '_load_constraints_from_json' "$PY_FILE" \
    && grep -q '_CONSTRAINTS_JSON_PATH' "$PY_FILE"; then
    check "Module loads via _load_constraints_from_json()" ok
else
    check "loader" fail "loader symbols missing"
fi

# 5) Container-side round-trip
roundtrip=$(docker exec "$LARAVEL_FA" python3 -c "
from app.agent.hallucination.layer6_constraints import GEOLOGICAL_CONSTRAINTS
expected = {
    'depth_max_m': (0.0, 5000.0),
    'grade_gold_max_ppm': (0.0, 1000.0),
    'grade_uranium_max_pct': (0.0, 50.0),
    'recovery_max_pct': (0.0, 100.0),
    'azimuth_range': (0.0, 360.0),
    'dip_range': (-90.0, 0.0),
    'rqd_range': (0.0, 100.0),
}
ok = True
for c in GEOLOGICAL_CONSTRAINTS:
    exp = expected.get(c.name)
    if exp is None or c.min_value != exp[0] or c.max_value != exp[1]:
        ok = False
        print('mismatch:', c.name, c.min_value, c.max_value)
        break
print('count:', len(GEOLOGICAL_CONSTRAINTS), 'match:', ok)
" 2>&1 | tail -1)
if [ "$roundtrip" = "count: 7 match: True" ]; then
    check "Container round-trip: 7 constraints with expected bounds" ok
else
    check "round-trip" fail "$roundtrip"
fi

# 6) End-to-end — _find_violations still catches a depth > 5000
violations=$(docker exec "$LARAVEL_FA" python3 -c "
from app.agent.hallucination.layer6_constraints import _find_violations
text = 'The drill hole reached a total depth of 9999 metres before stopping.'
vs = _find_violations(text)
print('violation_count:', len(vs))
if vs:
    print('first:', vs[0].constraint.name, vs[0].value)
" 2>&1 | tail -3)
if echo "$violations" | grep -q 'violation_count: 1' \
    && echo "$violations" | grep -q 'first: depth_max_m 9999'; then
    check "_find_violations still flags depth=9999m via the JSON-loaded constraint" ok
else
    check "end-to-end" fail "$(echo "$violations" | tr '\n' '|')"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
