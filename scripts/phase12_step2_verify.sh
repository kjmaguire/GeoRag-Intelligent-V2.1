#!/usr/bin/env bash
# =============================================================================
# scripts/phase12_step2_verify.sh
#
# Phase 12 Step 2 done-definition — inline prompt migration to
# prompts/ (R-P11-prompts-migrate).
#
# Migration target: _REPHRASE_SYSTEM_PROMPT in app/agent/escalation.py
#                 → app/agent/prompts/rephrase_system.py
#
#   1. New file src/fastapi/app/agent/prompts/rephrase_system.py
#      defines SYSTEM_PROMPT + PROMPT_VERSION
#   2. The inline triple-quoted definition is gone from escalation.py
#   3. escalation.py imports the prompt from its new home
#   4. _version_registry.py has a 'rephrase_system' entry
#   5. The migrated string round-trips: in-container
#      `_REPHRASE_SYSTEM_PROMPT == SYSTEM_PROMPT` is True
#   6. Pre-commit hook still matches the new file (system-prompt-
#      version-bump runs cleanly on it)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
PROMPT_FILE="$REPO/src/fastapi/app/agent/prompts/rephrase_system.py"
ESCALATION="$REPO/src/fastapi/app/agent/escalation.py"
REGISTRY="$REPO/src/fastapi/app/agent/prompts/_version_registry.py"
LARAVEL_FA="${FASTAPI_CONTAINER:-georag-fastapi}"
PRECOMMIT_BIN="${PRECOMMIT_BIN:-/home/georag/.local/share/uv/tools/pre-commit/bin/python}"

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
PHASE 12 STEP 2 — INLINE PROMPT MIGRATION VERIFICATION
============================================================
BANNER

# 1) Prompt file present + has expected constants
if [ -s "$PROMPT_FILE" ] \
    && grep -q '^SYSTEM_PROMPT = ' "$PROMPT_FILE" \
    && grep -q '^PROMPT_VERSION = ' "$PROMPT_FILE"; then
    check "rephrase_system.py exists with SYSTEM_PROMPT + PROMPT_VERSION" ok
else
    check "prompt file" fail "missing or incomplete"
fi

# 2) Inline triple-quoted definition gone from escalation.py
if grep -qE '^_REPHRASE_SYSTEM_PROMPT = """' "$ESCALATION"; then
    check "inline definition" fail "still present in escalation.py"
else
    check "Inline _REPHRASE_SYSTEM_PROMPT = \"\"\"... gone from escalation.py" ok
fi

# 3) escalation.py imports from new location
if grep -q 'from app.agent.prompts.rephrase_system import' "$ESCALATION"; then
    check "escalation.py imports from app.agent.prompts.rephrase_system" ok
else
    check "import" fail "escalation.py doesn't import the new path"
fi

# 4) Registry entry
if grep -qE '["'\''"]rephrase_system["'\''"]:' "$REGISTRY"; then
    check "rephrase_system registered in _version_registry.py" ok
else
    check "registry entry" fail "rephrase_system entry missing"
fi

# 5) Round-trip equality in-container
roundtrip=$(docker exec "$LARAVEL_FA" python3 -c "
from app.agent.prompts.rephrase_system import SYSTEM_PROMPT
from app.agent.escalation import _REPHRASE_SYSTEM_PROMPT
print('match' if _REPHRASE_SYSTEM_PROMPT == SYSTEM_PROMPT else 'mismatch')
" 2>&1 | tail -1)
[ "$roundtrip" = "match" ] \
    && check "Container-side _REPHRASE_SYSTEM_PROMPT == SYSTEM_PROMPT" ok \
    || check "round-trip" fail "$roundtrip"

# 6) Pre-commit hook still accepts the new file
pb=$(cd "$REPO" && "$PRECOMMIT_BIN" -m pre_commit run \
    system-prompt-version-bump \
    --files src/fastapi/app/agent/prompts/rephrase_system.py 2>&1)
if echo "$pb" | grep -q 'Passed'; then
    check "Pre-commit hook accepts the new prompt file" ok
else
    check "pre-commit hook" fail "$(echo "$pb" | tail -2 | tr '\n' '|')"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
