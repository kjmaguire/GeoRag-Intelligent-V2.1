#!/usr/bin/env bash
# scripts/create_session_prs.sh — 2026-06-03
#
# Walks the PR_PLAN_2026_06_03.md plan: creates 9 themed branches,
# commits the right file groups under YOUR git identity, optionally
# pushes + opens PRs.
#
# Why this script exists
# ----------------------
# The 2026-06-03 audit + recommendations session produced ~140
# modified/new files. A single mega-commit is unreviewable; a manual
# 9-PR split is tedious + error-prone (forget a file, wrong commit
# message, miss the deploy-ordering note). This script encodes the
# plan from docs/PR_PLAN_2026_06_03.md so the split is deterministic
# + repeatable.
#
# This script does NOT make commits under Claude's identity — it
# commits under YOUR `git config user.name/email`. The Co-Authored-By
# line attributes Claude appropriately.
#
# Usage:
#   bash scripts/create_session_prs.sh [--push] [--open-prs] [--dry-run]
#
#   --dry-run   Show what each step WOULD do; make no changes
#   --push      Push each branch to origin after committing (default off)
#   --open-prs  Run `gh pr create` for each branch after push (needs `gh auth login`)
#
# Recommended sequence:
#   1. bash scripts/create_session_prs.sh --dry-run   (sanity check)
#   2. bash scripts/create_session_prs.sh             (commit locally)
#   3. git log --oneline pr/01-audit-invariants-ci-gates  (review one)
#   4. bash scripts/create_session_prs.sh --push --open-prs  (ship)
#
# Idempotency: if a branch already exists the script skips it. Re-run
# safely after fixing a previous failure.

set -euo pipefail

# ─── Parse flags ────────────────────────────────────────────────────
DRY_RUN=0
PUSH=0
OPEN_PRS=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)   DRY_RUN=1 ;;
        --push)      PUSH=1 ;;
        --open-prs)  OPEN_PRS=1 ;;
        *) echo "Unknown flag: $arg"; exit 2 ;;
    esac
done

REMOTE="v21"  # PR target — matches the v2.1-baseline branch lineage
BASE_BRANCH="v2.1-baseline"

# Verify we're on the right base branch
CURRENT_BRANCH=$(git branch --show-current)
if [[ "$CURRENT_BRANCH" != "$BASE_BRANCH" ]]; then
    echo "FAIL: must run from $BASE_BRANCH (currently on $CURRENT_BRANCH)" >&2
    exit 2
fi

# Verify the identity is YOURS not the agent's
GIT_USER=$(git config --get user.name || echo "")
if [[ "$GIT_USER" == "Docker Agent" || "$GIT_USER" == "" || "$GIT_USER" == "claude" ]]; then
    echo "FAIL: git identity is '$GIT_USER' — set your real identity first:" >&2
    echo "  git config user.name 'Kyle Maguire'" >&2
    echo "  git config user.email 'kjmaguire@...'" >&2
    exit 2
fi

CO_AUTHOR_LINE="Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"

# Helper: run a command unless DRY_RUN.
run() {
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

# Helper: create a branch from $BASE_BRANCH, add the listed files, commit.
mk_pr() {
    local branch="$1"
    local subject="$2"
    local body="$3"
    shift 3
    local files=("$@")

    echo ""
    echo "═══ $branch ═══"
    echo "  subject: $subject"
    echo "  files: ${#files[@]}"

    if git show-ref --quiet "refs/heads/$branch"; then
        echo "  SKIP: branch $branch already exists"
        return 0
    fi

    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] would create branch $branch from $BASE_BRANCH"
        echo "  [dry-run] would stage:"
        for f in "${files[@]}"; do
            if [[ -e "$f" ]] || git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
                echo "    + $f"
            else
                echo "    ! MISSING: $f"
            fi
        done
        return 0
    fi

    git checkout -b "$branch" "$BASE_BRANCH"

    # Stage only the listed files (verify each exists or is tracked).
    for f in "${files[@]}"; do
        if [[ -e "$f" ]] || git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
            git add "$f" 2>&1 | grep -v "warning: in the working copy" || true
        else
            echo "  WARN: file $f doesn't exist + isn't tracked, skipping"
        fi
    done

    # Bail if nothing staged.
    if git diff --cached --quiet; then
        echo "  WARN: nothing staged for $branch — was the file list correct?"
        git checkout "$BASE_BRANCH"
        git branch -d "$branch"
        return 0
    fi

    git commit -m "$subject" -m "$body" -m "$CO_AUTHOR_LINE"
    echo "  ✓ committed"

    if [[ "$PUSH" == "1" ]]; then
        git push -u "$REMOTE" "$branch"
        echo "  ✓ pushed to $REMOTE"
    fi

    if [[ "$OPEN_PRS" == "1" ]]; then
        gh pr create \
            --title "$subject" \
            --body "$body" \
            --base "$BASE_BRANCH" \
            --head "$branch" \
            --repo "kjmaguire/GeoRag-Intelligent-V2.1"
        echo "  ✓ PR opened"
    fi

    git checkout "$BASE_BRANCH"
}

# ─── PR #1: REC#3 audit invariants CI gates ─────────────────────────
mk_pr \
    "pr/01-audit-invariants-ci-gates" \
    "chore(ci): audit invariants as CI gates (REC#3)" \
    "Converts every audit-derived regression test into a PR-time enforcement layer. See docs/AUDIT_INVARIANTS.md for the full index + docs/RECOMMENDATIONS_2026_06_03.md for the motivation (two audit-and-fix runs in 30 days finding the same bug classes)." \
    .github/workflows/audit-invariants.yml \
    .pre-commit-config.yaml \
    scripts/check_audit_invariants_php.sh \
    scripts/check_audit_invariants_python.sh \
    docs/AUDIT_INVARIANTS.md

# ─── PR #2: workspace_user pivot + Inertia share (item A) ───────────
mk_pr \
    "pr/02-workspace-user-pivot" \
    "feat(tenancy): workspace_user pivot + auth share (audit item A)" \
    "Single source of truth for user→workspace membership. Migration creates workspace_user, User->defaultWorkspaceId() helper, Inertia auth.user.workspaces share. HARD prerequisite for PR #7 (J: ProjectController) and PR #8 (REC foundations)." \
    database/migrations/2026_06_03_020000_create_workspace_user_table.php \
    database/migrations/2026_06_03_020100_provision_workspace_user_for_test_db.php \
    app/Models/Workspace.php \
    app/Models/User.php \
    app/Http/Middleware/HandleInertiaRequests.php \
    tests/Feature/Tenancy/WorkspaceUserMembershipTest.php

# ─── PR #3: silver.archive_ingest_runs (item C) ─────────────────────
mk_pr \
    "pr/03-archive-ingest-runs-observability" \
    "feat(observability): silver.archive_ingest_runs + on_failure_task (audit item C)" \
    "ZIP archive ingest gains parent-row lineage + failure backstop. Migration ordering: run \`php artisan migrate\` BEFORE restarting FastAPI so the workflow can import _archive_progress against an existing table." \
    database/migrations/2026_06_03_040000_create_silver_archive_ingest_runs.php \
    src/fastapi/app/hatchet_workflows/_archive_progress.py \
    src/fastapi/app/hatchet_workflows/ingest_zip_archive.py \
    src/fastapi/tests/test_ingest_zip_archive_observability.py \
    tests/Feature/Tenancy/ArchiveIngestRunsMigrationTest.php

# ─── PR #4: source-trust boost wiring (item D) ──────────────────────
mk_pr \
    "pr/04-source-trust-boost-wiring" \
    "feat(retrieval): source-trust boost wiring (audit item D)" \
    "Wires the dead boost_by_trust helper into the rerank path behind a phased-rollout flag. Default OFF. Rollout: flip ENABLED=true (SHADOW_MODE defaults true), watch georag_source_trust_boost_applied{mode=\"shadow\"} for a week, then flip SHADOW_MODE=false." \
    src/fastapi/app/config.py \
    src/fastapi/app/metrics.py \
    src/fastapi/app/agent/tools.py \
    src/fastapi/tests/test_source_trust_boost_wiring.py

# ─── PR #5: Martin role swap (item E) ───────────────────────────────
mk_pr \
    "pr/05-martin-readonly-role" \
    "feat(security): Martin role swap to martin_ro (audit item E)" \
    "Martin tile server drops georag_write privileges. Migration ordering: run \`php artisan migrate\` BEFORE the docker-compose restart that switches Martin to martin_ro — otherwise every tile request returns permission denied. Operational follow-up: rotate MARTIN_RO_PASSWORD per the docker-compose.yml playbook comment." \
    docker/postgresql/init/00-create-app-roles.sql \
    docker/postgresql/init/zz-grant-app-role-memberships.sql \
    database/migrations/2026_06_03_030000_grant_tile_functions_to_martin_ro.php \
    docker-compose.yml \
    .env.example \
    tests/Feature/Tenancy/MartinRoleReadOnlyTest.php

# ─── PR #6: per-workspace rate limits (item F) ──────────────────────
mk_pr \
    "pr/06-per-workspace-rate-limits" \
    "feat(rate-limit): per-workspace uploads + charts limiters (audit item F)" \
    "Edge throttling keyed on workspace_id (not user_id). Operator note: first heavy-upload day after deploy will hit throttle:uploads at 200/hr. Backfills should use the artisan ingest:reingest-project command (structurally exempt from middleware)." \
    app/Providers/AppServiceProvider.php \
    routes/api.php \
    tests/Feature/Tenancy/WorkspaceRateLimitsTest.php

# ─── PR #7: housekeeping G+H+I+J ────────────────────────────────────
mk_pr \
    "pr/07-housekeeping-g-h-i-j" \
    "chore: housekeeping (audit items G + H + I + J)" \
    "Four small cleanups: G deletes legacy Pages/Chat.tsx (1,585 lines), H tags 2 truly-dead settings (SUMMARIZER_ENABLED, TEMPERATURE_BY_QUERY_TYPE — the prior audit overcounted), I unskips 5 stale-skipped vLLM payload tests, J migrates ProjectController + OnboardingController to defaultWorkspaceId(). PR #7 has a HARD prerequisite on PR #2 (User->defaultWorkspaceId)." \
    resources/js/Pages/Chat.tsx \
    resources/js/Pages/__tests__/Chat.test.tsx \
    resources/js/Pages/Foundry/Chat.tsx \
    tests/Feature/Frontend/LegacyChatPageDeletedTest.php \
    src/fastapi/tests/test_dead_settings_tagged.py \
    src/fastapi/tests/test_vllm_payload_shape.py \
    app/Http/Controllers/Api/V1/ProjectController.php \
    app/Http/Controllers/OnboardingController.php \
    tests/Feature/Tenancy/ProjectCreationWorkspaceResolutionTest.php

# ─── PR #8: typed WorkspaceContext + scoped_connection foundations ──
mk_pr \
    "pr/08-typed-workspace-foundations" \
    "feat(arch): typed WorkspaceContext + scoped_connection foundations (REC#1 + REC#2 + REC#4 + REC#5)" \
    "The architectural recommendations Kyle blessed. REC#1 (typed FastAPI Depends), REC#2 (scoped_connection + bind_workspace_scope + ADR-0014 lookup_and_rescope, 38 bespoke sites migrated this session), REC#4 (settings lifecycle scaffold, 10/173), REC#5 (testcontainers PG scaffold). See docs/RECOMMENDATIONS_2026_06_03.md for the full status + docs/adr/0014-workspace-lookup-and-pivot.md for the two-phase scoping ADR. HARD prerequisite on PR #2 (workspace_user pivot)." \
    docs/RECOMMENDATIONS_2026_06_03.md \
    docs/adr/0014-workspace-lookup-and-pivot.md \
    src/fastapi/app/agent/workspace_context.py \
    src/fastapi/app/agent/workspace_dependency.py \
    src/fastapi/app/hatchet_workflows/_workspace_input.py \
    src/fastapi/app/db/__init__.py \
    src/fastapi/app/db/scoped_pool.py \
    src/fastapi/app/settings_lifecycle.py \
    src/fastapi/tests/conftest_pg.py \
    src/fastapi/tests/test_workspace_context.py \
    src/fastapi/tests/test_workspace_context_b4_centralisation.py \
    src/fastapi/tests/test_workspace_dependency.py \
    src/fastapi/tests/test_scoped_connection.py \
    src/fastapi/tests/test_lookup_and_rescope.py \
    scripts/verify_migration_ordering.sh

# Note: PR #8 also touches ~30 production files (B4 + REC#2 Phase-2
# migrations across agent/, services/, hatchet_workflows/, routers/).
# Those are split into PR #8b below for review reasons — REC #8 already
# touches enough new files; the migration sweeps go separately so
# reviewers can confirm "scoped_connection is wired correctly" in
# isolation from "boundary types are correct."

# ─── PR #8b: REC#2 Phase-2 migration sweep (production files) ───────
mk_pr \
    "pr/08b-rec2-phase2-migration-sweep" \
    "refactor(rls): REC#2 Phase-2 — 38 bespoke set_config sites → scoped_connection / bind_workspace_scope / lookup_and_rescope" \
    "Bulk migration of the 56→18 baseline. Every site uses one of the three canonical helpers. UUID validation on every pivot value. Cross-tenant elevations counted in WORKSPACE_RESOLUTION_FAILURES. See docs/adr/0014-workspace-lookup-and-pivot.md + the LEGACY_AWAITING_MIGRATION allowlist in tests/test_scoped_connection.py for the remaining 18 (all intentional — canonical helpers + audit save/restore + cockpit/4 + support_replay)." \
    src/fastapi/app/agent/agentic_retrieval/nodes.py \
    src/fastapi/app/agent/orchestrator/__init__.py \
    src/fastapi/app/agent/tools.py \
    src/fastapi/app/agent/entity_resolver.py \
    src/fastapi/app/agent/geospatial_planner.py \
    src/fastapi/app/agent/parent_expansion.py \
    src/fastapi/app/agent/project_geometry.py \
    src/fastapi/app/agents/phase0/support_packet.py \
    src/fastapi/app/agents/phase0/tenant_isolation_auditor.py \
    src/fastapi/app/agents/phase10/customer_response_drafting.py \
    src/fastapi/app/agents/phase10/escalation_routing.py \
    src/fastapi/app/agents/phase10/root_cause_investigation.py \
    src/fastapi/app/agents/phase10/support_packet.py \
    src/fastapi/app/agents/phase10/ticket_triage.py \
    src/fastapi/app/hatchet_workflows/_restore_pg_from_export.py \
    src/fastapi/app/hatchet_workflows/continuous_learning_loop.py \
    src/fastapi/app/hatchet_workflows/embed_pending_passages.py \
    src/fastapi/app/hatchet_workflows/embed_pending_passages_smoke.py \
    src/fastapi/app/hatchet_workflows/enrich_passage_context.py \
    src/fastapi/app/hatchet_workflows/field_outcome_learning.py \
    src/fastapi/app/hatchet_workflows/ingest_pdf.py \
    src/fastapi/app/hatchet_workflows/nightly_ingestion_integrity.py \
    src/fastapi/app/hatchet_workflows/ocr_quality_check.py \
    src/fastapi/app/hatchet_workflows/re_ocr_page.py \
    src/fastapi/app/hatchet_workflows/repair_shadow_aggregate.py \
    src/fastapi/app/hatchet_workflows/restore_workspace.py \
    src/fastapi/app/hatchet_workflows/shadow_diff.py \
    src/fastapi/app/hatchet_workflows/support_replay.py \
    src/fastapi/app/hatchet_workflows/sync_silver_to_kg.py \
    src/fastapi/app/hatchet_workflows/train_source_trust.py \
    src/fastapi/app/hatchet_workflows/train_target_model.py \
    src/fastapi/app/hatchet_workflows/what_changed_detector.py \
    src/fastapi/app/hatchet_workflows/workspace_export.py \
    src/fastapi/app/ocr/_persist.py \
    src/fastapi/app/routers/citation_feedback.py \
    src/fastapi/app/routers/visualizations.py \
    src/fastapi/app/services/claim_ledger.py \
    src/fastapi/app/services/geological_reasoning/hypothesis_generator.py \
    src/fastapi/app/services/ingest/cluster_runner.py \
    src/fastapi/app/services/ingest/context_enricher.py \
    src/fastapi/app/services/ingest/derive_intervals.py \
    src/fastapi/app/services/ingest/kg_sync.py \
    src/fastapi/app/services/ingest/passage_embedder.py \
    src/fastapi/app/services/ingest/tiff_ocr_ingester.py \
    src/fastapi/app/services/qdrant_fallback.py \
    src/fastapi/app/services/silver_dq_flag_writer.py \
    src/fastapi/app/services/support_cockpit/customer_response_drafting.py \
    src/fastapi/app/services/target_scoring_ml/shap_writer.py \
    src/fastapi/app/services/tool_gateway/gateway.py \
    src/fastapi/app/services/tool_gateway/impls.py \
    src/fastapi/app/services/trace_writer.py

# ─── PR #9: PR plan + recommendations docs ──────────────────────────
mk_pr \
    "pr/09-docs-pr-plan" \
    "docs: 2026-06-03 PR plan + recommendations" \
    "The plan doc that drove the 9-PR split + the recommendations status snapshot. Lands last so the rest are reviewable against the index." \
    docs/PR_PLAN_2026_06_03.md \
    docs/handover/AUDIT_AND_FIX_REPORT.md

echo ""
echo "═══ Done ═══"
if [[ "$DRY_RUN" == "1" ]]; then
    echo "Dry-run complete. No branches created. Re-run without --dry-run to commit."
elif [[ "$PUSH" == "0" ]]; then
    echo "9 branches created LOCALLY. Review with:"
    echo "  git log --oneline pr/01-audit-invariants-ci-gates"
    echo ""
    echo "Push them all when ready:"
    echo "  for b in \$(git branch --list 'pr/*' | tr -d ' *'); do git push -u $REMOTE \"\$b\"; done"
    echo ""
    echo "Or push + open PRs (requires gh auth login):"
    echo "  bash scripts/create_session_prs.sh --push --open-prs"
elif [[ "$OPEN_PRS" == "0" ]]; then
    echo "9 branches pushed to $REMOTE. Open PRs manually or re-run with --open-prs."
fi
