#!/usr/bin/env bash
# =============================================================================
# scripts/ci/langgraph_boundary_check.sh
#
# Master plan v2.4.2 §1 orchestration-boundary CI gate (kickoff #16).
#
# Hard rule from §1: LangGraph owns AI agent reasoning steps. It MUST NOT
# duplicate concerns owned by other orchestrators:
#
#   Hatchet           — multi-step durable workflow + retries + schedule
#   Dagster           — data pipelines with lineage
#   Kestra            — external SaaS / outbound webhook delivery
#   Laravel Horizon   — single Laravel-internal background job
#
# This check fails the build when LangGraph code tries to act like
# Hatchet (schedule, retry policy, durable state machine across hours
# of work) or like Kestra (outbound webhook with HMAC).
#
# Patterns flagged (case-insensitive):
#   LangGraph + (retry|schedule|cron|every_hours|interval=)  → use Hatchet
#   LangGraph + (webhook|http_post|outbound_call)            → use Kestra
#   LangGraph + (run_id|workflow_run|long_running)           → use Hatchet
#
# False-positive escape hatch: add  # langgraph-boundary-ok: <reason>
# on the offending line. The grep skips lines carrying that token.
#
# Exit 0 = clean. Exit 1 = boundary violations detected.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$HERE"

# Where LangGraph code lives (FastAPI services that wrap pydantic-ai or
# build state graphs over agent reasoning steps).
LANGGRAPH_PATHS=(
    "src/fastapi/app/services/report_builder"
    "src/fastapi/app/agent/pipeline"
    "src/fastapi/app/agent/orchestrator"
)

# Disallowed patterns + which orchestrator should own them.
# Format: <regex>|<owner>
PATTERNS=(
    "retry_policy|Hatchet"
    "schedule_cron|Hatchet"
    "cron_expression|Hatchet"
    "schedule_every|Hatchet"
    "@hatchet\.schedule|Hatchet (use directly, don't wrap in LangGraph)"
    "httpx\.AsyncClient.*post.*webhook|Kestra"
    "webhook_url.*hmac|Kestra"
    "long_running_workflow|Hatchet"
)

FOUND=0

echo "==> LangGraph boundary check (master plan §1)"
echo "    Scanning: ${LANGGRAPH_PATHS[*]}"

for path in "${LANGGRAPH_PATHS[@]}"; do
    [ -d "$path" ] || continue
    for entry in "${PATTERNS[@]}"; do
        pattern="${entry%%|*}"
        owner="${entry##*|}"
        hits=$(grep -rnE -i "$pattern" "$path" 2>/dev/null \
            | grep -v 'langgraph-boundary-ok:' \
            | grep -v '__pycache__' \
            | grep -v '\.pyc' || true)
        if [ -n "$hits" ]; then
            echo ""
            echo "  [VIOLATION] pattern: '$pattern' (belongs to: $owner)"
            echo "$hits" | sed 's/^/    /'
            FOUND=$((FOUND + 1))
        fi
    done
done

echo
if [ "$FOUND" -eq 0 ]; then
    echo "==> LangGraph boundary clean — 0 violations"
    exit 0
fi

echo "==> LangGraph boundary VIOLATED — $FOUND pattern(s) found"
echo "    Add '# langgraph-boundary-ok: <reason>' on the line to allow,"
echo "    or move the concern to the correct orchestrator."
exit 1
