#!/usr/bin/env bash
# scripts/open_all_prs.sh — 2026-06-24
#
# Opens the full PR stack for the pr/14 sweep + the retrieval-era slice, with
# correct base branches. Run AFTER `gh auth login`.
#
#   gh auth login
#   bash scripts/open_all_prs.sh [--draft]
#
# Idempotent-ish: `gh pr create` errors if a PR already exists for a branch;
# that error is caught and reported, the rest continue.

set -uo pipefail

DRAFT=""
[[ "${1:-}" == "--draft" ]] && DRAFT="--draft"

REPO="kjmaguire/GeoRag-Intelligent-V2.1"

if ! gh auth status >/dev/null 2>&1; then
    echo "FAIL: gh is not authenticated. Run 'gh auth login' first." >&2
    exit 2
fi

# Each w-branch is one themed commit on top of pr/14 → base = pr/14.
# --fill uses the (single, well-written) commit message as title + body.
WBRANCHES=(
    pr/w01-retrieval-quality-overhaul
    pr/w02-chatgpt-gap-import
    pr/w03-qdrant-chunks-schema
    pr/w04-cameco-ingest-throttle
    pr/w05-qwen-ecosystem-swap
    pr/w06-evidence-citation
    pr/w07-rls-tenancy
    pr/w08-shadow-observability
    pr/w09-monitoring-alerts
    pr/w10-foundry-frontend
    pr/w11-backend-housekeeping
)

open_pr() {
    local base="$1" head="$2"; shift 2
    echo ""
    echo "═══ $head  (base: $base) ═══"
    if gh pr view "$head" --repo "$REPO" >/dev/null 2>&1; then
        echo "  SKIP: a PR for $head already exists"
        return 0
    fi
    gh pr create --repo "$REPO" --base "$base" --head "$head" $DRAFT "$@" \
        || echo "  WARN: gh pr create failed for $head"
}

# 1. pr/14 itself — explicit title/body (don't --fill; its last commit is just
#    one of many). Targets the pr/13 base it was built on.
open_pr "pr/13-mechanical-followups" "pr/14-version-audit-updates" \
    --title "pr/14: version-audit sweep — image bumps, langgraph/langfuse fix, ADRs 0013-0017" \
    --body "Version-audit sweep + follow-ups. Full roadmap + the commit list: docs/handover/PR14_FOLLOWUPS.md. Includes the Unit-suite fix, PaddleOCR-VL Phase 2 parser (ADR-0016), and the Qwen3-VL shadow gate + dual-write (ADR-0015)."

# 2. The 11 retrieval-era slices — base pr/14, --fill from their commit.
for br in "${WBRANCHES[@]}"; do
    open_pr "pr/14-version-audit-updates" "$br" --fill
done

# 3. The traceparent test fix — base pr/14, --fill.
open_pr "pr/14-version-audit-updates" "fix/traceparent-test-reexport" --fill

echo ""
echo "Done. Review open PRs: gh pr list --repo $REPO"
