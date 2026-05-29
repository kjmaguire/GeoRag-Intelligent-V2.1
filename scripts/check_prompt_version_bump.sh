#!/usr/bin/env bash
# check_prompt_version_bump.sh
#
# Pre-commit hook: fails if any prompt-path file is staged without a
# corresponding bump to _SYSTEM_PROMPT_VERSION in orchestrator.py.
#
# Rationale (Module 5 Phase B PV-01):
#   _SYSTEM_PROMPT_VERSION is the human-readable marker for prompt evolution.
#   It is also used to invalidate Anthropic prompt caches and to version the
#   RETRIEVAL_STRATEGY_VERSION sub-minor string. Editing prompt text without
#   bumping the version causes silent prompt drift — cached entries from the
#   old version stay warm and may serve stale responses.
#
# Usage (manual test):
#   bash scripts/check_prompt_version_bump.sh
#
# Usage (via pre-commit):
#   pre-commit run system-prompt-version-bump --all-files
#
# Install (once Kyle approves pre-commit adoption):
#   pip install pre-commit
#   pre-commit install

set -euo pipefail

# Prompt-path patterns — any staged file matching these triggers the check.
ORCHESTRATOR="src/fastapi/app/agent/orchestrator.py"
PROMPT_PATTERN="^(src/fastapi/app/agent/orchestrator\.py|src/fastapi/app/prompts/|src/fastapi/app/agent/prompts/)"

# Detect staged files in prompt paths.
STAGED_PROMPTS=$(git diff --cached --name-only 2>/dev/null | grep -E "${PROMPT_PATTERN}" || true)

# If no prompt paths touched, pass immediately.
if [ -z "$STAGED_PROMPTS" ]; then
    exit 0
fi

# Prompt path(s) touched — require the version constant to have changed.
if git diff --cached "${ORCHESTRATOR}" 2>/dev/null | grep -qE "^[+-].*_SYSTEM_PROMPT_VERSION[[:space:]]*="; then
    # Version line is in the diff — bump confirmed.
    echo "OK: _SYSTEM_PROMPT_VERSION bump detected alongside prompt edits."
    exit 0
fi

# Version NOT bumped — fail the commit.
echo ""
echo "ERROR: Prompt file(s) staged without _SYSTEM_PROMPT_VERSION bump."
echo ""
echo "Staged prompt files:"
echo "${STAGED_PROMPTS}" | sed 's/^/  /'
echo ""
echo "Any edit to a prompt-path file must be accompanied by an increment"
echo "to _SYSTEM_PROMPT_VERSION in ${ORCHESTRATOR}."
echo ""
echo "Why this matters:"
echo "  - Anthropic prompt cache uses the literal text of cache_control"
echo "    blocks. An unchanged version constant means stale cached entries"
echo "    may continue serving the old prompt for up to the cache TTL."
echo "  - RETRIEVAL_STRATEGY_VERSION (query_classifier.py) should also be"
echo "    bumped (sub-minor) when prompt content changes — see the v2.1"
echo "    example in that file."
echo ""
echo "Fix: increment _SYSTEM_PROMPT_VERSION in ${ORCHESTRATOR}, then re-stage."
exit 1
