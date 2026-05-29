#!/usr/bin/env bash
# =============================================================================
# scripts/phase11_step5_verify.sh
#
# Phase 11 Step 5 done-definition — pre-commit hook end-to-end
# activation. Phase 5 Step 3 added the hook config; Phase 11 Step 3
# created the prompts/ subdirectory the hook watches. This verifier
# proves the chain is now actually live.
#
#   1. .git/hooks/pre-commit script installed
#   2. uv-managed `pre-commit` binary reachable
#   3. `pre-commit run` against a fastapi-services file passes
#      (fastapi-pydantic-freshness hook fires + accepts)
#   4. `pre-commit run` against a prompts/ file passes
#      (system-prompt-version-bump hook fires + accepts, since the
#      file isn't part of an actual staged-but-no-version-bump diff)
#   5. Both configured hooks have id strings in
#      .pre-commit-config.yaml
#   6. The prompt-bump hook's `files` regex still matches the
#      prompts/ subdirectory (Phase 11 Step 3 confirms the path)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
PRECOMMIT_BIN="${PRECOMMIT_BIN:-/home/georag/.local/share/uv/tools/pre-commit/bin/python}"
HOOK_SCRIPT="$REPO/.git/hooks/pre-commit"
CONFIG="$REPO/.pre-commit-config.yaml"

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
PHASE 11 STEP 5 — PRE-COMMIT HOOK END-TO-END ACTIVATION
============================================================
BANNER

# 1) Git hook script installed
if [ -x "$HOOK_SCRIPT" ] && grep -q 'pre-commit' "$HOOK_SCRIPT"; then
    check ".git/hooks/pre-commit script installed + executable" ok
else
    check "git hook" fail "missing or not executable: $HOOK_SCRIPT"
fi

# 2) Pre-commit binary reachable
if [ -x "$PRECOMMIT_BIN" ]; then
    pc_ver=$("$PRECOMMIT_BIN" -m pre_commit --version 2>&1 | tr -d '\r')
    check "pre-commit binary reachable ($pc_ver)" ok
else
    check "binary" fail "$PRECOMMIT_BIN missing"
fi

# 3) freshness hook accepts a fastapi-services file
fr=$(cd "$REPO" && "$PRECOMMIT_BIN" -m pre_commit run \
    fastapi-pydantic-freshness \
    --files src/fastapi/app/services/flow_jwt.py 2>&1)
if echo "$fr" | grep -q 'Passed'; then
    check "fastapi-pydantic-freshness hook fires + Passed on a services file" ok
else
    check "freshness hook" fail "$(echo "$fr" | tail -3 | tr '\n' '|')"
fi

# 4) prompt-bump hook accepts a prompts/ file
pb=$(cd "$REPO" && "$PRECOMMIT_BIN" -m pre_commit run \
    system-prompt-version-bump \
    --files src/fastapi/app/agent/prompts/example_system.py 2>&1)
if echo "$pb" | grep -q 'Passed'; then
    check "system-prompt-version-bump hook fires + Passed on a prompts/ file" ok
else
    check "prompt hook" fail "$(echo "$pb" | tail -3 | tr '\n' '|')"
fi

# 5) Both hook ids present in config
ids_present=0
for id in fastapi-pydantic-freshness system-prompt-version-bump; do
    grep -q "id: $id" "$CONFIG" && ids_present=$((ids_present + 1))
done
[ "$ids_present" = "2" ] \
    && check "Both hook ids configured in .pre-commit-config.yaml" ok \
    || check "config ids" fail "only $ids_present / 2 ids present"

# 6) Prompt hook's files regex still matches prompts/ subdirectory
# (the regex is on the YAML `files:` line of the prompt hook block)
if grep -E 'prompts/.\*' "$CONFIG" | grep -q files; then
    check "Prompt hook's files regex still matches prompts/ tree" ok
else
    # Fall back to simpler match if line ordering varies
    if grep -q 'prompts/\\.\\*\|app/agent/prompts' "$CONFIG"; then
        check "Prompt hook's files regex still matches prompts/ tree" ok
    else
        check "prompts regex" fail "pattern not present in config"
    fi
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
