#!/usr/bin/env bash
# scripts/create_retrieval_era_prs.sh — 2026-06-24
#
# Slices the uncommitted 2026-06-01 → 06-03 retrieval-era WIP into ~11 themed
# branches off v2.1-baseline, committed under YOUR identity. Mirrors the proven
# scripts/create_session_prs.sh (which sliced the audit work into pr/01…pr/09).
#
# See docs/PR_PLAN_2026_06_24_retrieval_era_wip.md for the rationale, the
# file→branch table, and the caveats (cross-cutting main.py / validators.py
# hunks, wiring coupling, .claude exclusion, test-DB migration siblings).
#
# Usage:
#   bash scripts/create_retrieval_era_prs.sh [--push] [--open-prs] [--dry-run]
#     --dry-run   Show the file→branch mapping; make no changes
#     --push      Push each branch to the v21 remote after committing
#     --open-prs  gh pr create for each branch after push (needs gh auth login)
#
# Recommended: --dry-run first, then no-flag (local commits), review, then
# --push --open-prs. Idempotent: existing branches are skipped.

set -euo pipefail

DRY_RUN=0; PUSH=0; OPEN_PRS=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)  DRY_RUN=1 ;;
        --push)     PUSH=1 ;;
        --open-prs) OPEN_PRS=1 ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

REMOTE="v21"
# Base = pr/14, NOT v2.1-baseline: this WIP was authored on the pr/11→14
# lineage, so files touched by both those commits AND the WIP cannot be
# replayed onto v2.1-baseline (git checkout aborts on the conflict). Basing
# here is conflict-free and faithful; the trade-off is the WIP PRs are stacked
# on pr/14 rather than an independent fan. See PR_PLAN for the full rationale.
BASE_BRANCH="pr/14-version-audit-updates"
CO_AUTHOR_LINE="Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"

CURRENT_BRANCH=$(git branch --show-current)
if [[ "$DRY_RUN" == "0" && "$CURRENT_BRANCH" != "$BASE_BRANCH" ]]; then
    echo "FAIL: must run from $BASE_BRANCH (currently on $CURRENT_BRANCH)." >&2
    echo "  git checkout $BASE_BRANCH   # your uncommitted WIP carries over" >&2
    exit 2
fi

GIT_USER=$(git config --get user.name || echo "")
if [[ "$DRY_RUN" == "0" ]]; then
    if [[ "$GIT_USER" == "Docker Agent" || "$GIT_USER" == "" || "$GIT_USER" == "claude" ]]; then
        echo "FAIL: git identity is '$GIT_USER' — set your real identity first:" >&2
        echo "  git config user.name 'Kyle Maguire'" >&2
        echo "  git config user.email 'kjmaguire@...'" >&2
        exit 2
    fi
fi

# Track every file assigned to a branch so the leftovers report is accurate.
ASSIGNED_FILES=()

# Helper: create $branch from $BASE_BRANCH, stage the listed files, commit.
mk_pr() {
    local branch="$1" subject="$2" body="$3"
    shift 3
    local files=("$@")
    ASSIGNED_FILES+=("${files[@]}")

    echo ""
    echo "═══ $branch ═══"
    echo "  subject: $subject"
    echo "  files:   ${#files[@]}"

    if git show-ref --quiet "refs/heads/$branch"; then
        echo "  SKIP: branch $branch already exists"
        return 0
    fi

    if [[ "$DRY_RUN" == "1" ]]; then
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
    for f in "${files[@]}"; do
        if git check-ignore -q -- "$f" 2>/dev/null; then
            echo "  WARN: $f is gitignored — skipping (git add -f if intended)"
            continue
        fi
        if [[ -e "$f" ]] || git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
            # `|| echo` so one bad add can't abort the whole run under `set -e`.
            git add -- "$f" || echo "  WARN: git add failed for $f"
        else
            echo "  WARN: $f doesn't exist + isn't tracked — skipping"
        fi
    done

    if git diff --cached --quiet; then
        echo "  WARN: nothing staged for $branch — leaving branch empty"
    else
        git commit -m "$subject" -m "$body" -m "$CO_AUTHOR_LINE"
    fi

    if [[ "$PUSH" == "1" ]]; then
        git push -u "$REMOTE" "$branch"
        if [[ "$OPEN_PRS" == "1" ]]; then
            gh pr create --base "$BASE_BRANCH" --head "$branch" \
                --title "$subject" --body "$body" || \
                echo "  WARN: gh pr create failed for $branch (auth? base?)"
        fi
    fi

    git checkout "$BASE_BRANCH"
}

# ─────────────────────────────────────────────────────────────────────
mk_pr "pr/w01-retrieval-quality-overhaul" \
    "feat(retrieval): multi-query expansion + decomposition + citation-first salvage + grounding" \
    "The 2026-06-01/02 answer-pathway overhaul. Flag-gated; see OVERNIGHT_2026_06_02.md for the pass-rate trajectory (+7pp cross-project) and the flag matrix." \
    src/fastapi/app/services/multi_query_expansion.py \
    src/fastapi/app/services/multi_project_decomposition.py \
    src/fastapi/app/services/atomic_claim_extractor.py \
    src/fastapi/app/services/sentence_grounding.py \
    src/fastapi/app/services/corpus_summarizer.py \
    src/fastapi/app/services/query_classifier.py \
    src/fastapi/app/models/rag.py \
    src/fastapi/app/services/eval/validators.py \
    src/fastapi/app/agent/llm_calls.py \
    src/fastapi/app/agent/deps.py \
    src/fastapi/tests/test_multi_project_decomposition.py \
    src/fastapi/tests/test_multi_query_and_citation_parsers.py \
    src/fastapi/tests/test_retrieval_quality.py \
    OVERNIGHT_2026_06_02.md

mk_pr "pr/w02-chatgpt-gap-import" \
    "feat(eval): import 1500 ChatGPT gap questions into golden set" \
    "GapImportCsvSeeder + question_set CHECK extension + the two source CSVs (1000 mixed-project + 500 export)." \
    database/seeders/GapImportCsvSeeder.php \
    database/migrations/2026_06_01_120000_extend_golden_questions_set_check.php \
    tests/golden_questions/csv_imports/gap_questions_1000.csv \
    tests/golden_questions/csv_imports/questions_500_export.csv

mk_pr "pr/w03-qdrant-chunks-schema" \
    "fix(retrieval): georag_chunks sparse slot + payload audit workflow" \
    "init_qdrant recreates the collection with the sparse 'text' slot; qdrant_payload_audit detects minimal-payload rogue points." \
    src/fastapi/scripts/init_qdrant.py \
    src/fastapi/app/hatchet_workflows/qdrant_payload_audit.py

mk_pr "pr/w04-cameco-ingest-throttle" \
    "feat(ingestion): upload-path Hatchet throttle + --keys-file reingest" \
    "Cameco-recovery resilience: HatchetDispatchThrottle guards GROUP_ROUND_ROBIN saturation; ReingestProject gains --keys-file + qdrant-default." \
    app/Console/Commands/Ingestion/ReingestProject.php \
    app/Services/Ingestion/HatchetDispatchThrottle.php \
    app/Http/Controllers/Api/V1/UploadController.php \
    app/Http/Controllers/Api/V1/DrillUploadController.php \
    tests/Feature/Ingestion/HatchetDispatchThrottleTest.php \
    tests/Feature/Ingestion/ReingestProjectKeysFileTest.php \
    tests/Feature/Ingestion/ReingestProjectQdrantDefaultTest.php

mk_pr "pr/w05-qwen-ecosystem-swap" \
    "feat(ml): Qwen3 reranker swap + embedding runbook + drop-ollama backend check" \
    "reranker.py moves to Qwen3-Reranker; embedding-swap-qwen3 runbook; answer_runs backend CHECK drops the retired 'ollama' enum value." \
    src/fastapi/app/services/reranker.py \
    docs/runbooks/embedding-swap-qwen3.md \
    database/migrations/2026_06_02_220000_drop_ollama_from_answer_runs_backend_check.php

mk_pr "pr/w06-evidence-citation" \
    "feat(chat): evidence controller + query-persist-failure event + inspector UI" \
    "EvidenceController + QueryPersistFailure broadcast event + EvidenceInspector panel + citation-feedback wiring + channel auth." \
    app/Http/Controllers/Api/V1/EvidenceController.php \
    app/Events/QueryPersistFailure.php \
    resources/js/Components/chat/EvidenceInspector.tsx \
    app/Http/Controllers/CitationFeedbackController.php \
    routes/channels.php \
    tests/Feature/Tenancy/WorkspaceActivityChannelAuthTest.php

mk_pr "pr/w07-rls-tenancy" \
    "fix(tenancy): repair GUC RLS policies + tenancy regression tests" \
    "Replaces the broken-GUC fail-open policy on gold.repair_shadow_daily; closes targeting-workflow RLS gaps; adds the no-legacy-GUC PHP guard + RLS coverage test." \
    database/migrations/2026_05_29_201500_replace_broken_guc_rls_policy_gold_repair_shadow_daily.php \
    database/migrations/2026_06_03_010000_close_targeting_workflow_rls_gaps.php \
    tests/Feature/Tenancy/NoLegacyGucSetConfigInPhpTest.php \
    tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php

mk_pr "pr/w08-shadow-observability" \
    "feat(observability): shadow-trigger + answer-quality scoring instrumentation" \
    "shadow_trigger + score_answer_quality observability with a regression test." \
    src/fastapi/app/routers/shadow_trigger.py \
    src/fastapi/app/hatchet_workflows/score_answer_quality.py \
    src/fastapi/tests/test_shadow_trigger_observability.py

mk_pr "pr/w09-monitoring-alerts" \
    "chore(monitoring): vLLM + watchdog alert rules + alertmanager + bucket init" \
    "Prometheus vllm/watchdog rules, alertmanager routing, MinIO/SeaweedFS bucket creation tweaks." \
    docker/prometheus/rules/vllm-alerts.yml \
    docker/prometheus/rules/watchdog.yml \
    docker/alertmanager/alertmanager.yml \
    docker/minio/create-buckets.sh

mk_pr "pr/w10-foundry-frontend" \
    "feat(foundry): NewProject UI + Foundry/PublicApi/Trust controller updates" \
    "NewProject.tsx form changes + Portfolio/ProjectsIndex/Settings/Tier3 + InterpretationWorkspace + PublicApi + Trust controllers." \
    resources/js/Pages/Foundry/NewProject.tsx \
    app/Http/Controllers/Foundry/PortfolioController.php \
    app/Http/Controllers/Foundry/ProjectsIndexController.php \
    app/Http/Controllers/Foundry/SettingsController.php \
    app/Http/Controllers/Foundry/Tier3Controller.php \
    app/Http/Controllers/InterpretationWorkspaceController.php \
    app/Http/Controllers/Api/V1/PublicApiController.php \
    app/Http/Controllers/Api/V1/TrustController.php

mk_pr "pr/w11-backend-housekeeping" \
    "chore: lifespan wiring + assorted backend/docs leftovers" \
    "CAUTION: main.py + escalation_routing carry cross-cutting hunks — review the diff (see PR_PLAN caveats). Includes handover docs, seeders, dagster/bench tweaks, and the planning artifacts." \
    src/fastapi/app/main.py \
    src/fastapi/app/hatchet_workflows/worker.py \
    src/fastapi/app/hatchet_workflows/tiff_normalize.py \
    src/fastapi/app/agent/figure_extractor.py \
    src/fastapi/app/agent/public_geoscience_tool.py \
    src/fastapi/app/agents/phase0/store_reconciliation.py \
    src/fastapi/app/services/support_cockpit/escalation_routing.py \
    src/dagster/georag_dagster/resources.py \
    scripts/bench_500q_overnight.py \
    database/seeders/CgiVocabSeeder.php \
    database/raw/phase0/README.md \
    COVERAGE.md \
    docs/handover/DFS.md \
    docs/handover/HANDOVER_INDEX.md \
    docs/handover/REPORT_GAP_ANALYSIS.md \
    docs/handover/SAD.md \
    scripts/create_session_prs.sh \
    scripts/create_retrieval_era_prs.sh \
    docs/PR_PLAN_2026_06_24_retrieval_era_wip.md

# ─── Leftovers report ────────────────────────────────────────────────
echo ""
echo "═══ leftovers (uncommitted + unassigned) ═══"
ASSIGNED_SORTED=$(printf '%s\n' "${ASSIGNED_FILES[@]}" | sort -u)
# All tracked-modified + untracked files, excluding local agent state.
ALL_WIP=$( { git diff --name-only; git ls-files --others --exclude-standard; } \
    | grep -vE '^\.claude/worktrees/' | sort -u )
LEFTOVERS=$(comm -23 <(echo "$ALL_WIP") <(echo "$ASSIGNED_SORTED") || true)
if [[ -z "$LEFTOVERS" ]]; then
    echo "  none — every WIP file is assigned to a branch."
else
    echo "  the following WIP files are NOT in any branch above:"
    echo "$LEFTOVERS" | sed 's/^/    /'
    echo "  → add them to a group in this script or commit them deliberately."
fi

echo ""
echo "Done. Next:"
echo "  git log --oneline pr/w01-retrieval-quality-overhaul"
echo "  bash scripts/create_retrieval_era_prs.sh --push --open-prs   # when ready"
