#!/usr/bin/env bash
# =============================================================================
# scripts/phase11_step3_verify.sh
#
# Phase 11 Step 3 done-definition — prompts/ subdirectory bootstrap
# (R-P11-C).
#
#   1. src/fastapi/app/agent/prompts/ directory exists
#   2. __init__.py, _version_registry.py, and example_system.py all
#      present
#   3. example_system.py defines SYSTEM_PROMPT + PROMPT_VERSION
#   4. _version_registry.py defines a PROMPT_REGISTRY dict with the
#      example_system entry
#   5. `from app.agent.prompts import PROMPT_REGISTRY,
#      EXAMPLE_SYSTEM_PROMPT, EXAMPLE_PROMPT_VERSION` works inside
#      the fastapi container
#   6. .pre-commit-config.yaml watches the prompts/ subdirectory
#      (was set up in Phase 5 Step 3; verify it still matches)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
PROMPTS="$REPO/src/fastapi/app/agent/prompts"
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
PHASE 11 STEP 3 — PROMPTS BOOTSTRAP VERIFICATION
============================================================
BANNER

# 1) Directory
if [ -d "$PROMPTS" ]; then
    check "prompts/ subdirectory exists" ok
else
    check "directory" fail "$PROMPTS missing"
fi

# 2) Required files
missing=()
for f in __init__.py _version_registry.py example_system.py; do
    [ -f "$PROMPTS/$f" ] || missing+=("$f")
done
if [ "${#missing[@]}" -eq 0 ]; then
    check "All three bootstrap files present" ok
else
    check "files" fail "missing: ${missing[*]}"
fi

# 3) example_system.py shape
if grep -q '^SYSTEM_PROMPT = ' "$PROMPTS/example_system.py" \
    && grep -q '^PROMPT_VERSION = ' "$PROMPTS/example_system.py"; then
    check "example_system.py defines SYSTEM_PROMPT + PROMPT_VERSION" ok
else
    check "example shape" fail "constants missing"
fi

# 4) Registry contains the example (accept either quote style)
if grep -qE "['\"]example_system['\"]" "$PROMPTS/_version_registry.py" \
    && grep -q 'PROMPT_REGISTRY' "$PROMPTS/_version_registry.py"; then
    check "_version_registry.py contains example_system entry" ok
else
    check "registry shape" fail "PROMPT_REGISTRY or example entry missing"
fi

# 5) In-container import
import_out=$(docker exec "$LARAVEL_FA" python3 -c "
from app.agent.prompts import (
    PROMPT_REGISTRY, EXAMPLE_SYSTEM_PROMPT, EXAMPLE_PROMPT_VERSION,
)
keys = list(PROMPT_REGISTRY.keys())
print('keys:', keys)
print('version:', EXAMPLE_PROMPT_VERSION)
print('prompt_chars:', len(EXAMPLE_SYSTEM_PROMPT))
" 2>&1 | tail -3)
if echo "$import_out" | grep -qE "keys: \[.*example_system" \
    && echo "$import_out" | grep -q 'version: 0.1.0' \
    && echo "$import_out" | grep -qE 'prompt_chars: [0-9]+'; then
    check "Container-side import resolves all three symbols" ok
else
    check "import" fail "$(echo "$import_out" | tr '\n' '|')"
fi

# 6) Pre-commit watches the prompts/ tree (Phase 5 Step 3 wiring)
if grep -q 'prompts/.\*' "$REPO/.pre-commit-config.yaml"; then
    check ".pre-commit-config.yaml watches the prompts/ subdirectory" ok
else
    check "pre-commit match" fail "regex pattern not present"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
