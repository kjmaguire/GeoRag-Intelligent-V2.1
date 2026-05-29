#!/usr/bin/env bash
# =============================================================================
# scripts/phase14_step1_verify.sh
#
# Phase 14 Step 1 — AGENT_SYSTEM_PROMPT migration.
#
# Note: agentic_escalation.py has an unrelated pydantic-ai / anthropic
# SDK import-time error in this dev image. The verifier therefore
# round-trips via the canonical prompts module rather than the consumer
# (the consumer's source-level import line is statically checked).
#
#   1. agent_system.py exists with SYSTEM_PROMPT + PROMPT_VERSION
#   2. Inline 67-line block removed from agentic_escalation.py
#   3. agentic_escalation.py imports from app.agent.prompts.agent_system
#   4. Registry has 'agent_system' entry (now 4 total: example/rephrase/classifier/agent)
#   5. Prompt module importable; SYSTEM_PROMPT length non-trivial (>3kB)
#   6. Pre-commit hook accepts the new prompt file
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
PROMPT_FILE="$REPO/src/fastapi/app/agent/prompts/agent_system.py"
CONSUMER="$REPO/src/fastapi/app/agent/agentic_escalation.py"
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
PHASE 14 STEP 1 — AGENT_SYSTEM_PROMPT MIGRATION
============================================================
BANNER

# 1) Prompt file shape
if [ -s "$PROMPT_FILE" ] \
    && grep -q '^SYSTEM_PROMPT = ' "$PROMPT_FILE" \
    && grep -q '^PROMPT_VERSION = ' "$PROMPT_FILE"; then
    check "agent_system.py exists with SYSTEM_PROMPT + PROMPT_VERSION" ok
else
    check "prompt file" fail "missing or incomplete"
fi

# 2) Inline string removed
if grep -qE '^_AGENT_SYSTEM_PROMPT = """' "$CONSUMER"; then
    check "inline definition" fail "still present"
else
    check "Inline _AGENT_SYSTEM_PROMPT triple-quoted string removed" ok
fi

# 3) Consumer imports
if grep -q 'from app.agent.prompts.agent_system import' "$CONSUMER"; then
    check "agentic_escalation.py imports from app.agent.prompts.agent_system" ok
else
    check "import" fail "consumer doesn't import new path"
fi

# 4) Registry entry — must include the 4 original Phase 11-14 keys.
# Phase 33+ migrations (orchestrator_shared_preamble_dash etc.) add
# more entries; the verifier accepts any superset of the original 4
# so re-running Phase 14 verification against a Phase 33+ tree
# doesn't false-fail.
keys=$(docker exec "$LARAVEL_FA" python3 -c "
from app.agent.prompts._version_registry import PROMPT_REGISTRY
print(','.join(sorted(PROMPT_REGISTRY.keys())))
" 2>&1 | tail -1)
required="agent_system classifier_system example_system rephrase_system"
missing=""
for k in $required; do
    case ",$keys," in
        *",$k,"*) ;;
        *) missing="$missing $k" ;;
    esac
done
if [ -z "$missing" ]; then
    check "Registry contains all 4 Phase 11-14 entries (got [$keys])" ok
else
    check "registry" fail "missing:$missing (got [$keys])"
fi

# 5) Prompt importable + non-trivial length
import_out=$(docker exec "$LARAVEL_FA" python3 -c "
from app.agent.prompts.agent_system import SYSTEM_PROMPT, PROMPT_VERSION
print(len(SYSTEM_PROMPT), PROMPT_VERSION)
" 2>&1 | tail -1)
chars=$(echo "$import_out" | awk '{print $1}')
if [ "${chars:-0}" -gt 3000 ] 2>/dev/null; then
    check "Prompt module imports + SYSTEM_PROMPT is non-trivial ($chars chars)" ok
else
    check "import + size" fail "got '$import_out'"
fi

# 6) Pre-commit hook
pb=$(cd "$REPO" && "$PRECOMMIT_BIN" -m pre_commit run \
    system-prompt-version-bump \
    --files src/fastapi/app/agent/prompts/agent_system.py 2>&1)
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
