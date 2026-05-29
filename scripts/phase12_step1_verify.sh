#!/usr/bin/env bash
# =============================================================================
# scripts/phase12_step1_verify.sh
#
# Phase 12 Step 1 done-definition — hallucination init.py docstring
# drift fix (R-P11-init-drift).
#
#   1. The "handled elsewhere" / "not implemented" wording for
#      Layers 2 and 5 is gone
#   2. Docstring now references the actual layer2_typed_output.py
#      (128 lines) + layer5_provenance.py (157 lines) implementations
#   3. Docstring references the Phase 11 audit doc as the trail
#   4. __init__.py still parses + imports cleanly
#   5. Imported symbols (verify_numerical_claims etc.) still resolve
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
INIT="$REPO/src/fastapi/app/agent/hallucination/__init__.py"
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
PHASE 12 STEP 1 — HALLUCINATION INIT DOCSTRING DRIFT FIX
============================================================
BANNER

# 1) The "handled elsewhere" claim is gone (or has been re-framed as
# historical note within a "drift corrected" section).
stale_count=$(grep -cE 'Layers 2 and 5 are handled elsewhere' "$INIT" || true)
[ "$stale_count" = "0" ] \
    && check "Stale 'Layers 2 and 5 are handled elsewhere' wording removed" ok \
    || check "stale wording" fail "still present $stale_count time(s)"

# 2) Both layer files referenced with line counts in the new docstring
if grep -q 'layer2_typed_output.py' "$INIT" \
    && grep -q 'layer5_provenance.py' "$INIT" \
    && grep -q '128 lines' "$INIT" \
    && grep -q '157 lines' "$INIT"; then
    check "Updated docstring references both layer files + line counts" ok
else
    check "layer refs" fail "missing layer file refs or line counts"
fi

# 3) Trail back to the Phase 11 audit
if grep -q 'phase11_section_04i_audit.md' "$INIT"; then
    check "Docstring references the Phase 11 audit doc as trail" ok
else
    check "audit ref" fail "Phase 11 audit doc not referenced"
fi

# 4) AST parses
if python3 -c "import ast; ast.parse(open('$INIT').read())" 2>/dev/null; then
    check "__init__.py parses cleanly" ok
else
    check "ast parse" fail "syntax error"
fi

# 5) In-container import still resolves all three exported symbols
import_out=$(docker exec "$LARAVEL_FA" python3 -c "
from app.agent.hallucination import (
    verify_numerical_claims,
    resolve_entity_references,
    check_geological_constraints,
)
print('all-three-OK')
" 2>&1 | tail -1)
[ "$import_out" = "all-three-OK" ] \
    && check "All three exported validators still importable" ok \
    || check "imports" fail "$import_out"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
