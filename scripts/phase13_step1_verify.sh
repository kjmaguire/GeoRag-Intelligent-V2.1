#!/usr/bin/env bash
# =============================================================================
# scripts/phase13_step1_verify.sh
#
# Phase 13 Step 1 — second inline prompt migration (classifier).
# Same shape as Phase 12 Step 2's rephrase_system migration.
#
#   1. classifier_system.py exists with SYSTEM_PROMPT + PROMPT_VERSION
#   2. Inline triple-quoted definition removed from llm_classifier.py
#   3. llm_classifier.py imports from the new location
#   4. Registry has 'classifier_system' entry
#   5. Round-trip equality: _CLASSIFIER_SYSTEM_PROMPT == SYSTEM_PROMPT
#   6. Pre-commit hook accepts the new prompt file
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
PROMPT_FILE="$REPO/src/fastapi/app/agent/prompts/classifier_system.py"
CONSUMER="$REPO/src/fastapi/app/agent/llm_classifier.py"
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
PHASE 13 STEP 1 — CLASSIFIER PROMPT MIGRATION
============================================================
BANNER

# 1) Prompt file shape
if [ -s "$PROMPT_FILE" ] \
    && grep -q '^SYSTEM_PROMPT = ' "$PROMPT_FILE" \
    && grep -q '^PROMPT_VERSION = ' "$PROMPT_FILE"; then
    check "classifier_system.py exists with SYSTEM_PROMPT + PROMPT_VERSION" ok
else
    check "prompt file" fail "missing or incomplete"
fi

# 2) Inline string gone
if grep -qE '^_CLASSIFIER_SYSTEM_PROMPT = """' "$CONSUMER"; then
    check "inline definition" fail "still present in llm_classifier.py"
else
    check "Inline _CLASSIFIER_SYSTEM_PROMPT triple-quoted string removed" ok
fi

# 3) Consumer imports
if grep -q 'from app.agent.prompts.classifier_system import' "$CONSUMER"; then
    check "llm_classifier.py imports from app.agent.prompts.classifier_system" ok
else
    check "import" fail "consumer doesn't import new path"
fi

# 4) Registry entry
if grep -qE '["'\''"]classifier_system["'\''"]:' "$REGISTRY"; then
    check "classifier_system registered in _version_registry.py" ok
else
    check "registry entry" fail "missing"
fi

# 5) Round-trip equality
roundtrip=$(docker exec "$LARAVEL_FA" python3 -c "
from app.agent.prompts.classifier_system import SYSTEM_PROMPT
from app.agent.llm_classifier import _CLASSIFIER_SYSTEM_PROMPT
print('match' if _CLASSIFIER_SYSTEM_PROMPT == SYSTEM_PROMPT else 'mismatch')
" 2>&1 | tail -1)
[ "$roundtrip" = "match" ] \
    && check "Container-side _CLASSIFIER_SYSTEM_PROMPT == SYSTEM_PROMPT" ok \
    || check "round-trip" fail "$roundtrip"

# 6) Pre-commit hook
pb=$(cd "$REPO" && "$PRECOMMIT_BIN" -m pre_commit run \
    system-prompt-version-bump \
    --files src/fastapi/app/agent/prompts/classifier_system.py 2>&1)
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
