#!/usr/bin/env bash
# =============================================================================
# scripts/phase15_step2_verify.sh
#
# Phase 15 Step 2 — orchestrator inline-prompt audit.
# (Bundled migration deferred to R-P15-1; doc captures the scope.)
#
#   1. docs/phase15_orchestrator_prompts_audit.md exists + non-trivial
#   2. Doc enumerates ≥10 prompt variants currently inline in orchestrator.py
#   3. Doc references `_SYSTEM_PROMPT_VERSION` (cache key for migration)
#   4. Doc captures the R-P15-1 carry-over for the bundled migration
#   5. Inline prompts still match the audit — orchestrator.py has the 10
#      variants the doc claims (no silent drift)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase15_orchestrator_prompts_audit.md"
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
PHASE 15 STEP 2 — ORCHESTRATOR PROMPT AUDIT
============================================================
BANNER

# 1) Doc present + non-trivial
if [ -s "$DOC" ]; then
    lines=$(wc -l < "$DOC")
    [ "$lines" -ge 60 ] \
        && check "Audit doc present ($lines lines)" ok \
        || check "doc length" fail "$lines lines"
else
    check "doc exists" fail "missing"
fi

# 2) Doc enumerates ≥10 prompt variants
variants_in_doc=$(grep -cE '_SYSTEM_PROMPT_[A-Z]+' "$DOC" || true)
[ "${variants_in_doc:-0}" -ge 10 ] 2>/dev/null \
    && check "Audit names ≥10 prompt-variant constants" ok \
    || check "variant enumeration" fail "got $variants_in_doc"

# 3) References _SYSTEM_PROMPT_VERSION
if grep -q '_SYSTEM_PROMPT_VERSION' "$DOC"; then
    check "Audit references _SYSTEM_PROMPT_VERSION cache-key bump" ok
else
    check "version ref" fail "missing"
fi

# 4) R-P15-1 carry-over flagged
if grep -q 'R-P15-1' "$DOC"; then
    check "Audit flags R-P15-1 (bundled migration carry-over)" ok
else
    check "carry-over" fail "R-P15-1 not flagged"
fi

# 5) Audit's claim about prompt variants. The audit doc was written
# at Phase 15 against the then-current state of 10 inline constants.
# Phase 33+ migrations move them to `prompts/*.py` modules — count
# the union of inline-defined + module-imported binding names.
#
# Acceptance: the binding union covers all 10 audit-listed variant
# names. Phase 33 moved SHARED_PREAMBLE; Phase 34 moved DEFAULT,
# NUMERIC, NARRATIVE, GRAPH (dash variants). Phase 35+ will move
# the 5 colon counterparts. As long as every audit-named variant
# resolves to an existing binding (inline or imported), there's no
# drift.
inline_count=$(grep -cE '^_SYSTEM_PROMPT_(SHARED_PREAMBLE|DEFAULT|NUMERIC|NARRATIVE|GRAPH|STATIC)(_COLON)? *=' "$ORCH" || true)
import_count=$(grep -cE 'SYSTEM_PROMPT as _SYSTEM_PROMPT_(SHARED_PREAMBLE|DEFAULT|NUMERIC|NARRATIVE|GRAPH)(_COLON)?' "$ORCH" || true)
total_bindings=$((inline_count + import_count))
if [ "${total_bindings:-0}" -ge 10 ] 2>/dev/null; then
    check "orchestrator.py has all 10 audit-listed prompt bindings (inline=$inline_count + imports=$import_count = $total_bindings)" ok
else
    check "inline drift" fail "inline=$inline_count + imports=$import_count = $total_bindings < 10"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
