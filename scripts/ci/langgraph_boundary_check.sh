#!/usr/bin/env bash
# =============================================================================
# scripts/ci/langgraph_boundary_check.sh
#
# Master plan v2.4.2 §1 orchestration-boundary CI gate (kickoff #16).
#
# Hard rule from §1: LangGraph owns AI agent reasoning steps. It MUST NOT
# duplicate concerns owned by other orchestrators:
#
#   Hatchet           — multi-step durable workflow + retries + schedule +
#                       outbound webhook delivery (via the `outbox` schema's
#                       external_webhook target — HMAC-signed, retried,
#                       dead-lettered; see app/hatchet_workflows/outbox_dispatcher.py)
#   Laravel Horizon   — single Laravel-internal background job
#
# Kestra was retired 2026-07-28 (A7) — it was never actually deployed, so
# "outbound webhook via Kestra" was already a fiction this script enforced.
# The real owner of that concern was always Hatchet + the outbox dispatcher.
# Dagster's data-pipeline role was retired 2026-07-28 (B2).
#
# This check fails the build when LangGraph code tries to act like
# Hatchet: schedule, retry policy, durable state machine across hours of
# work, or outbound webhook delivery with HMAC (all Hatchet's job via the
# outbox, not LangGraph's).
#
# Patterns flagged (case-insensitive):
#   LangGraph + (retry|schedule|cron|every_hours|interval=)  → use Hatchet
#   LangGraph + (webhook|http_post|outbound_call)            → use Hatchet + outbox
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

# Where LangGraph code lives: every module under the FastAPI app that mentions
# LangGraph, derived on each run. This was a hand-kept list of three
# directories. One of them, agent/pipeline, no longer exists and `[ -d ] ||
# continue` skipped it without a word, and the packages that actually build
# state graphs (agent/agentic_retrieval, services/target_recommendation,
# services/llm_incident_diagnosis) were never on it -- so the gate scanned a
# fraction of the code it names and printed "clean". Deriving the list means a
# new LangGraph module is covered the day it lands.
mapfile -t LANGGRAPH_FILES < <(grep -rilI --include='*.py' langgraph src/fastapi/app 2>/dev/null \
    | grep -v '__pycache__' | sort)

# Disallowed patterns + which orchestrator should own them.
# Format: <regex>|<owner>
PATTERNS=(
    "retry_policy|Hatchet"
    "schedule_cron|Hatchet"
    "cron_expression|Hatchet"
    "schedule_every|Hatchet"
    "@hatchet\.schedule|Hatchet (use directly, don't wrap in LangGraph)"
    "httpx\.AsyncClient.*post.*webhook|Hatchet + outbox (app/hatchet_workflows/outbox_dispatcher.py)"
    "webhook_url.*hmac|Hatchet + outbox (app/hatchet_workflows/outbox_dispatcher.py)"
    "long_running_workflow|Hatchet"
)

FOUND=0

echo "==> LangGraph boundary check (master plan §1)"

# A scan of nothing is not a pass. If no module mentions LangGraph, either it
# was removed (then retire this gate) or the search above stopped finding it
# (then this gate has been reporting "clean" about nothing).
if [ "${#LANGGRAPH_FILES[@]}" -eq 0 ]; then
    echo "==> LangGraph boundary check FAILED — found no module under src/fastapi/app"
    echo "    that mentions LangGraph, so there is nothing to scan."
    exit 1
fi
echo "    Scanning: ${#LANGGRAPH_FILES[@]} module(s) under src/fastapi/app that mention LangGraph"

for entry in "${PATTERNS[@]}"; do
    pattern="${entry%%|*}"
    owner="${entry##*|}"
    hits=$(grep -nHE -i "$pattern" "${LANGGRAPH_FILES[@]}" 2>/dev/null \
        | grep -v 'langgraph-boundary-ok:' || true)
    if [ -n "$hits" ]; then
        echo ""
        echo "  [VIOLATION] pattern: '$pattern' (belongs to: $owner)"
        echo "$hits" | sed 's/^/    /'
        FOUND=$((FOUND + 1))
    fi
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
