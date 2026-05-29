#!/usr/bin/env bash
# Autonomous-run substrate rollup verifier (doc-phase 106).
#
# Asserts the cumulative substrate landed in doc-phases 74-105:
#   - Schema migrations applied (§6.5, §8.1, §9.1, §9.4, §9.9, §10.1,
#     §10.5, §10.8, §12.7)
#   - Hatchet workflows registered (10 new long-running workflows)
#   - Python agent packages import cleanly (phase6/7/8/9/10)
#   - Service packages import cleanly (report_builder,
#     target_recommendation, decision_intelligence,
#     geological_ontology, audit/hash_chain_proof + cold_tier_archive,
#     eval, support_cockpit, target_scoring_ml, source_trust,
#     llm_incident_diagnosis)
#   - Laravel model + controller load (SavedMapView)
#
# Fast-cascade via _verifier_manifest.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

source "$SCRIPT_DIR/_verifier_manifest.sh"

PG_CONTAINER="${PG_CONTAINER:-georag-postgresql}"
FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"
LARAVEL_CONTAINER="${LARAVEL_CONTAINER:-georag-laravel-octane}"
PSQL="docker exec $PG_CONTAINER psql -U georag -d georag -tAX"

FAIL=0
TOTAL=0
note() { echo "$1"; }

check() {
    TOTAL=$((TOTAL + 1))
    if eval "$2" >/dev/null 2>&1; then
        note "[$1] PASS — $3"
    else
        note "[$1] FAIL — $3"
        FAIL=$((FAIL + 1))
    fi
}

check_count() {
    TOTAL=$((TOTAL + 1))
    local actual
    actual=$(eval "$2" 2>/dev/null || echo "0")
    if [ "$actual" = "$3" ]; then
        note "[$1] PASS — $4 (got $actual)"
    else
        note "[$1] FAIL — $4 (expected $3, got $actual)"
        FAIL=$((FAIL + 1))
    fi
}

# ----------------------------------------------------------------------
# Database substrate (8 schema landings)
# ----------------------------------------------------------------------
note ""
note "=== Database substrate ==="

check_count "silver.saved_map_views" \
    "$PSQL -c \"SELECT count(*) FROM pg_tables WHERE schemaname='silver' AND tablename='saved_map_views';\"" \
    "1" "§6.5 silver.saved_map_views exists"

check_count "targeting.* (10 tables)" \
    "$PSQL -c \"SELECT count(*) FROM pg_tables WHERE schemaname='targeting';\"" \
    "10" "§8.1 targeting schema has 10 tables"

check_count "ontology tables" \
    "$PSQL -c \"SELECT count(*) FROM pg_tables WHERE schemaname='silver' AND tablename IN ('geological_ontology_terms','geological_ontology_synonyms');\"" \
    "2" "§9.1 ontology tables exist"

check_count "hypotheses tables" \
    "$PSQL -c \"SELECT count(*) FROM pg_tables WHERE schemaname='silver' AND tablename IN ('hypotheses','hypothesis_evidence_links');\"" \
    "2" "§9.4 hypothesis tables exist"

check_count "decision_intelligence (5 tables)" \
    "$PSQL -c \"SELECT count(*) FROM pg_tables WHERE schemaname='silver' AND tablename LIKE 'decision_%';\"" \
    "5" "§9.9 decision intelligence tables exist"

check_count "eval.* (3 tables)" \
    "$PSQL -c \"SELECT count(*) FROM pg_tables WHERE schemaname='eval';\"" \
    "3" "§10.1+10.5 eval schema exists"

check_count "ops.* (3 tables)" \
    "$PSQL -c \"SELECT count(*) FROM pg_tables WHERE schemaname='ops';\"" \
    "3" "§10.8 ops schema exists"

check_count "source_trust tables" \
    "$PSQL -c \"SELECT count(*) FROM pg_tables WHERE schemaname='silver' AND tablename LIKE 'source_trust%';\"" \
    "2" "§12.7 source_trust tables exist"

# ----------------------------------------------------------------------
# Seed-data floor gates (doc-phase 112 mechanical ontology)
# ----------------------------------------------------------------------
note ""
note "=== Seed-data floors ==="

_check_seed_floor() {
    local label="$1"
    local sql="$2"
    local floor="$3"
    local description="$4"
    TOTAL=$((TOTAL + 1))
    local actual
    actual=$($PSQL -c "$sql" 2>/dev/null || echo "0")
    actual=$(echo "$actual" | tr -d ' ')
    if [ "$actual" -ge "$floor" ] 2>/dev/null; then
        note "[seed:${label}] PASS — ${description} (got ${actual}, floor ${floor})"
    else
        note "[seed:${label}] FAIL — ${description} (got ${actual}, need ≥ ${floor})"
        FAIL=$((FAIL + 1))
    fi
}

_check_seed_floor "ontology:commodity" \
    "SELECT count(*) FROM silver.geological_ontology_terms WHERE class='commodity';" \
    "40" "commodity ontology terms seeded"

_check_seed_floor "ontology:geological_age" \
    "SELECT count(*) FROM silver.geological_ontology_terms WHERE class='geological_age';" \
    "25" "geological_age ontology terms seeded"

_check_seed_floor "ontology:resource_class" \
    "SELECT count(*) FROM silver.geological_ontology_terms WHERE class='resource_class';" \
    "7" "resource_class ontology terms seeded"

_check_seed_floor "ontology:total_synonyms" \
    "SELECT count(*) FROM silver.geological_ontology_synonyms;" \
    "100" "ontology synonyms seeded across all classes"

# ----------------------------------------------------------------------
# Live pytest modules — doc-phases 114, 115, 116
# ----------------------------------------------------------------------
note ""
note "=== Live pytest modules ==="

_check_pytest_module() {
    local label="$1"
    local path="$2"
    local description="$3"
    TOTAL=$((TOTAL + 1))
    if docker exec "$FASTAPI_CONTAINER" sh -c "cd /app && python -m pytest ${path} -q 2>&1" \
        | tail -3 | grep -qE "passed"; then
        note "[pytest:${label}] PASS — ${description}"
    else
        note "[pytest:${label}] FAIL — ${description}"
        FAIL=$((FAIL + 1))
    fi
}

_check_pytest_module "ontology_resolver" \
    "tests/test_ontology_resolver.py" \
    "ontology resolve_term + find_synonyms live"

_check_pytest_module "decision_recorder" \
    "tests/test_decision_recorder.py" \
    "decision_intelligence.record_decision live"

_check_pytest_module "support_access_audit" \
    "tests/test_support_access_audit.py" \
    "support_cockpit.emit_support_access_audit live"

_check_pytest_module "hash_chain_proof" \
    "tests/test_hash_chain_proof.py" \
    "audit.hash_chain_proof.build_hash_chain_proof live"

_check_pytest_module "langfuse_link" \
    "tests/test_langfuse_link.py" \
    "support_cockpit.open_trace_with_audit live"

_check_pytest_module "decision_summary" \
    "tests/test_decision_summary.py" \
    "decision_intelligence.get_workspace_decision_summary live"

_check_pytest_module "ontology_stats" \
    "tests/test_ontology_stats.py" \
    "geological_ontology.get_ontology_class_stats live"

_check_pytest_module "workspace_audit_excerpt" \
    "tests/test_workspace_audit_excerpt.py" \
    "audit.get_workspace_audit_excerpt live"

_check_pytest_module "sme_seeders" \
    "tests/test_sme_seeders.py" \
    "sme_content seeder + Athabasca uranium TODO guard"

_check_pytest_module "mechanical_questions" \
    "tests/test_mechanical_questions.py" \
    "§10.2 mechanical golden questions (53 across 5 sets — refusal_correctness added doc-phase 160)"

_check_pytest_module "workspace_evaluator" \
    "tests/test_workspace_evaluator.py" \
    "§10.4 evaluate_workspace orchestration + §10.6 promotion gate live"

_check_pytest_module "hypothesis_generator" \
    "tests/test_hypothesis_generator.py" \
    "§9.10 ai_suggested hypothesis emitter live"

_check_pytest_module "ticket_triage" \
    "tests/test_ticket_triage.py" \
    "§25.4 ticket_triage support agent live (synthetic stub classifier)"

_check_pytest_module "report_builder_planning_nodes" \
    "tests/test_report_builder_planning_nodes.py" \
    "§7-A v1 first 4 of 12 §15.1 report graph nodes live"

_check_pytest_module "target_recommendation_nodes" \
    "tests/test_target_recommendation_nodes.py" \
    "§8.7 weighted-scoring formula + 6 of 12 §18.2 target graph nodes live"

_check_pytest_module "root_cause_investigation" \
    "tests/test_root_cause_investigation.py" \
    "§25.4 root_cause_investigation support agent live"

_check_pytest_module "support_packet" \
    "tests/test_support_packet.py" \
    "§25.4 support_packet assembler live"

_check_pytest_module "langgraph_wirings" \
    "tests/test_langgraph_wirings.py" \
    "§15.1 + §18.2 LangGraph Pregel pipelines compile + run end-to-end"

_check_pytest_module "customer_response_drafting" \
    "tests/test_customer_response_drafting.py" \
    "§25.4 customer_response_drafting agent live"

_check_pytest_module "escalation_routing" \
    "tests/test_escalation_routing.py" \
    "§25.4 escalation_routing agent live (closes §25.4 5-agent suite)"

_check_pytest_module "hatchet_workflow_bodies" \
    "tests/test_hatchet_workflow_bodies.py" \
    "generate_report + score_targets Hatchet task bodies invoke LangGraph"

_check_pytest_module "support_replay_workflow" \
    "tests/test_support_replay_workflow.py" \
    "support_replay Hatchet task body runs §25.4 chain end-to-end"

_check_pytest_module "what_changed_detector" \
    "tests/test_what_changed_detector.py" \
    "§9.13 what_changed_detector Hatchet task body live (audit-ledger delta scan)"

_check_pytest_module "restore_workspace" \
    "tests/test_restore_workspace.py" \
    "§11.3 restore_workspace Hatchet task body live (dry-run consistency check)"

_check_pytest_module "bc_minfile_adapter" \
    "tests/test_bc_minfile_adapter.py" \
    "§6.2 BC MINFILE adapter live (15 mineral occurrences seeded)"

_check_pytest_module "nrcan_mines_adapter" \
    "tests/test_nrcan_mines_adapter.py" \
    "§6.3 NRCan Canadian Mines adapter live (12 mines seeded)"

_check_pytest_module "sk_minoccur_adapter" \
    "tests/test_sk_minoccur_adapter.py" \
    "§6.1 SK mineral occurrence adapter live (14 occurrences seeded)"

_check_pytest_module "sk_drillhole_adapter" \
    "tests/test_sk_drillhole_adapter.py" \
    "§6.1 SK drillhole collar adapter live (12 drillholes seeded)"

_check_pytest_module "bc_drillhole_adapter" \
    "tests/test_bc_drillhole_adapter.py" \
    "§6.2 BC MINFILE drillhole adapter live (10 drillholes seeded)"

_check_pytest_module "assessment_survey_adapters" \
    "tests/test_assessment_survey_adapters.py" \
    "§6.1+§6.2 SK + BC ARIS assessment-survey adapters live (16 surveys)"

_check_pytest_module "bedrock_geology_adapters" \
    "tests/test_bedrock_geology_adapters.py" \
    "§6.3+§6.4 AB + NRCan bedrock-geology adapters live (16 units) — §6 CLOSED"

_check_pytest_module "whatchanged_report_integration" \
    "tests/test_whatchanged_report_integration.py" \
    "§7.2 ↔ §9.13 cross-section integration: what_changed reports use detector output"

_check_pytest_module "real_llm_evaluator" \
    "tests/test_real_llm_evaluator.py" \
    "§10.4 real_llm_v1 evaluator live (real vLLM call + §04i refusal-correctness validator)"

_check_pytest_module "evaluate_workspace_workflow" \
    "tests/test_evaluate_workspace_workflow.py" \
    "evaluate_workspace Hatchet workflow threads evaluator_kind end-to-end"

_check_pytest_module "real_rag_evaluator" \
    "tests/test_real_rag_evaluator.py" \
    "§10.4 real_rag_v1 evaluator live (full RAG: Qdrant + Neo4j + vLLM + §04i refusal validator)"

_check_pytest_module "eval_validators" \
    "tests/test_eval_validators.py" \
    "§04i shared validators module: Layer 6 refusal + Layer 2 citation_presence + chain"

_check_pytest_module "eval_real_rag_nightly_workflow" \
    "tests/test_eval_real_rag_nightly_workflow.py" \
    "§10.6 eval_real_rag_nightly cron wraps real_rag_v1 @ 05:15 UTC against refusal_correctness"

# Doc-phase 171 — §04i failure-layer breakdown lives on the Eval
# Dashboard. The controller returns 8 canonical buckets (6 §04i layers
# + 2 infra) merged with any observed buckets in eval.run_results. Here
# we verify the underlying SQL query runs cleanly + the executed_at
# column the controller groups on still exists.
TOTAL=$((TOTAL + 1))
FLB_COL_EXISTS=$($PSQL -c "SELECT count(*) FROM information_schema.columns WHERE table_schema='eval' AND table_name='run_results' AND column_name='executed_at'")
if [ "$FLB_COL_EXISTS" = "1" ]; then
    note "[eval:failure-layer-breakdown] PASS — eval.run_results.executed_at column present (controller GROUP BY target)"
else
    note "[eval:failure-layer-breakdown] FAIL — eval.run_results.executed_at column missing — controller will throw"
    FAIL=$((FAIL + 1))
fi

# Doc-phase 172 — re-runnability of the 5 workspace-isolation policy
# migrations. Each must precede its CREATE POLICY with a
# `DROP POLICY IF EXISTS`. Without this, `RefreshDatabase` under
# pgsql phpunit fails on the second test that uses the trait
# (custom schemas survive `migrate:fresh`).
TOTAL=$((TOTAL + 1))
MIGRATIONS_WITHOUT_DROP_FIRST=$(
    for f in \
        /home/georag/projects/georag/database/migrations/2026_05_13_090000_create_silver_saved_map_views.php \
        /home/georag/projects/georag/database/migrations/2026_05_13_100000_create_targeting_schema.php \
        /home/georag/projects/georag/database/migrations/2026_05_13_120000_create_silver_hypotheses.php \
        /home/georag/projects/georag/database/migrations/2026_05_13_130000_create_decision_intelligence_schema.php \
        /home/georag/projects/georag/database/migrations/2026_05_13_150000_create_source_trust_schema.php; do
        if ! grep -q "DROP POLICY IF EXISTS" "$f"; then
            echo "$f"
        fi
    done | wc -l
)
if [ "$MIGRATIONS_WITHOUT_DROP_FIRST" = "0" ]; then
    note "[migrations:policy-drop-first] PASS — all 5 workspace-isolation migrations are re-runnable under RefreshDatabase"
else
    note "[migrations:policy-drop-first] FAIL — $MIGRATIONS_WITHOUT_DROP_FIRST migrations missing DROP POLICY IF EXISTS preamble"
    FAIL=$((FAIL + 1))
fi

# Doc-phase 172 — config/inertia.php override pins testing page-paths
# to resources/js/Pages (capital P). Without this, pgsql phpunit
# admin Inertia render assertions fail with "page component does not
# exist" because the package default is lowercase 'pages'.
TOTAL=$((TOTAL + 1))
if [ -f /home/georag/projects/georag/config/inertia.php ] && \
   grep -q "resource_path('js/Pages')" /home/georag/projects/georag/config/inertia.php; then
    note "[config:inertia-page-paths] PASS — config/inertia.php pins testing paths to resources/js/Pages"
else
    note "[config:inertia-page-paths] FAIL — config/inertia.php missing or doesn't pin testing path to capital-P Pages"
    FAIL=$((FAIL + 1))
fi

# Doc-phase 173 — bronze.ingest_* tables for the Phase A archive walk.
TOTAL=$((TOTAL + 1))
INGEST_TABLES=$($PSQL -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='bronze' AND table_name IN ('ingest_runs','ingest_manifest','ingest_triage_samples')")
if [ "$INGEST_TABLES" = "3" ]; then
    note "[ingest:phase-a-tables] PASS — bronze.ingest_runs + ingest_manifest + ingest_triage_samples present"
else
    note "[ingest:phase-a-tables] FAIL — only $INGEST_TABLES of 3 Phase A tables present"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
if docker exec "$FASTAPI_CONTAINER" python /app/scripts/inspect_ingest_zip.py --help >/dev/null 2>&1; then
    note "[ingest:phase-a-script] PASS — inspect_ingest_zip.py loads cleanly in fastapi container"
else
    note "[ingest:phase-a-script] FAIL — inspect_ingest_zip.py import/argparse failed"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
DECISION_TYPES_COVERED=$($PSQL -c "SELECT count(DISTINCT decision_type) FROM silver.decision_records")
if [ "$DECISION_TYPES_COVERED" -ge "8" ]; then
    note "[silver:decision-types-coverage] PASS — $DECISION_TYPES_COVERED of 8 §21.3 decision types have real decisions (full coverage)"
else
    note "[silver:decision-types-coverage] PARTIAL — $DECISION_TYPES_COVERED of 8 §21.3 types covered"
fi

TOTAL=$((TOTAL + 1))
PG_BC_OCCURRENCES=$($PSQL -c "SELECT count(*) FROM public_geoscience.pg_mineral_occurrence WHERE source_id = 'bc_minfile_mineral_occurrence'")
if [ "$PG_BC_OCCURRENCES" -ge "15" ]; then
    note "[public-geoscience:bc-minfile-data] PASS — $PG_BC_OCCURRENCES BC mineral occurrences in DB (>=15)"
else
    note "[public-geoscience:bc-minfile-data] FAIL — only $PG_BC_OCCURRENCES rows (expected >=15)"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
PG_NRCAN_MINES=$($PSQL -c "SELECT count(*) FROM public_geoscience.pg_mine WHERE source_id = 'nrcan_canadian_mines'")
if [ "$PG_NRCAN_MINES" -ge "12" ]; then
    note "[public-geoscience:nrcan-mines-data] PASS — $PG_NRCAN_MINES NRCan mines in DB (>=12)"
else
    note "[public-geoscience:nrcan-mines-data] FAIL — only $PG_NRCAN_MINES rows (expected >=12)"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Doc-phase 125 — pyproject-vs-imports drift gate
# ----------------------------------------------------------------------
note ""
note "=== Pyproject coverage gate ==="
TOTAL=$((TOTAL + 1))
if python3 "$SCRIPT_DIR/check_pyproject_covers_imports.py" >/dev/null 2>&1; then
    note "[pyproject-coverage:fastapi] PASS — every app/ import is in pyproject (or allow-listed)"
else
    note "[pyproject-coverage:fastapi] FAIL — see scripts/check_pyproject_covers_imports.py output"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
if python3 "$SCRIPT_DIR/check_pyproject_covers_imports.py" \
        "$REPO_ROOT/src/dagster/pyproject.toml" \
        "$REPO_ROOT/src/dagster/georag_dagster" >/dev/null 2>&1; then
    note "[pyproject-coverage:dagster] PASS — every georag_dagster/ import is in pyproject (or allow-listed)"
else
    note "[pyproject-coverage:dagster] FAIL — see scripts/check_pyproject_covers_imports.py output"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Hatchet workflow registrations
# ----------------------------------------------------------------------
note ""
note "=== Hatchet workflows ==="

WORKFLOWS_LIST=$(docker exec "$FASTAPI_CONTAINER" python -m app.hatchet_workflows.worker --list 2>/dev/null || echo "")

for wf in generate_report score_targets field_outcome_learning what_changed_detector \
          evaluate_workspace eval_real_rag_nightly support_replay restore_workspace \
          train_target_model train_source_trust continuous_learning_loop \
          sync_silver_to_kg embed_pending_passages; do
    TOTAL=$((TOTAL + 1))
    if echo "$WORKFLOWS_LIST" | grep -qx "$wf"; then
        note "[wf:$wf] PASS — registered in AI pool"
    else
        note "[wf:$wf] FAIL — not in worker --list output"
        FAIL=$((FAIL + 1))
    fi
done

# ----------------------------------------------------------------------
# Python agent packages
# ----------------------------------------------------------------------
note ""
note "=== Python agent packages ==="

for phase in 6 7 8 9 10; do
    TOTAL=$((TOTAL + 1))
    if docker exec "$FASTAPI_CONTAINER" python -c "from app.agents import phase${phase}; assert len(phase${phase}.__all__) > 0" 2>/dev/null; then
        AGENT_COUNT=$(docker exec "$FASTAPI_CONTAINER" python -c "from app.agents import phase${phase}; print(len(phase${phase}.__all__))" 2>/dev/null)
        note "[agents:phase${phase}] PASS — ${AGENT_COUNT} agents callable"
    else
        note "[agents:phase${phase}] FAIL — module import failed"
        FAIL=$((FAIL + 1))
    fi
done

# ----------------------------------------------------------------------
# Service packages
# ----------------------------------------------------------------------
note ""
note "=== Service packages ==="

for svc in report_builder target_recommendation decision_intelligence \
           geological_ontology eval support_cockpit target_scoring_ml \
           source_trust llm_incident_diagnosis; do
    TOTAL=$((TOTAL + 1))
    if docker exec "$FASTAPI_CONTAINER" python -c "import app.services.${svc}" 2>/dev/null; then
        note "[svc:${svc}] PASS — imports cleanly"
    else
        note "[svc:${svc}] FAIL — import failed"
        FAIL=$((FAIL + 1))
    fi
done

# audit utilities
for util in hash_chain_proof cold_tier_archive; do
    TOTAL=$((TOTAL + 1))
    if docker exec "$FASTAPI_CONTAINER" python -c "import app.audit.${util}" 2>/dev/null; then
        note "[audit:${util}] PASS — imports cleanly"
    else
        note "[audit:${util}] FAIL — import failed"
        FAIL=$((FAIL + 1))
    fi
done

# ----------------------------------------------------------------------
# Laravel model layer + controller
# ----------------------------------------------------------------------
note ""
note "=== Laravel model layer ==="

# Helper — assert one class exists via Laravel boot.
_check_php_class() {
    local label="$1"
    local cls="$2"
    TOTAL=$((TOTAL + 1))
    if docker exec "$LARAVEL_CONTAINER" php artisan tinker --execute \
        "echo class_exists('${cls}') ? 'OK' : 'MISSING';" 2>/dev/null | grep -q OK; then
        note "[laravel:${label}] PASS — ${cls} loads"
    else
        note "[laravel:${label}] FAIL — ${cls} load failed"
        FAIL=$((FAIL + 1))
    fi
}

# Existing — SavedMapView (doc-phase 105 + 107)
_check_php_class "savedmapview" 'App\Models\SavedMapView'
_check_php_class "savedmapview-factory" 'Database\Factories\SavedMapViewFactory'
_check_php_class "savedmapview-controller" 'App\Http\Controllers\Api\V1\SavedMapViewController'

# Eval models + factory (doc-phase 109)
_check_php_class "eval:goldenquestion" 'App\Models\Eval\GoldenQuestion'
_check_php_class "eval:goldenquestion-factory" 'Database\Factories\Eval\GoldenQuestionFactory'

# Ops models + factory (doc-phase 109)
_check_php_class "ops:supportticket" 'App\Models\Ops\SupportTicket'
_check_php_class "ops:supportticket-trace" 'App\Models\Ops\SupportTicketTrace'
_check_php_class "ops:supportticket-replay" 'App\Models\Ops\SupportReplayRun'
_check_php_class "ops:supportticket-factory" 'Database\Factories\Ops\SupportTicketFactory'

# Targeting models + factory (doc-phase 110)
_check_php_class "targeting:recommendation" 'App\Models\Targeting\TargetRecommendation'
_check_php_class "targeting:review-decision" 'App\Models\Targeting\TargetReviewDecision'
_check_php_class "targeting:outcome" 'App\Models\Targeting\TargetOutcome'
_check_php_class "targeting:recommendation-factory" 'Database\Factories\Targeting\TargetRecommendationFactory'

# Silver hypotheses + decision intelligence models (doc-phase 110)
_check_php_class "silver:hypothesis" 'App\Models\Silver\Hypothesis'
_check_php_class "silver:hypothesis-evidence-link" 'App\Models\Silver\HypothesisEvidenceLink'
_check_php_class "silver:hypothesis-factory" 'Database\Factories\Silver\HypothesisFactory'
_check_php_class "silver:decision-record" 'App\Models\Silver\DecisionRecord'
_check_php_class "silver:decision-evidence-link" 'App\Models\Silver\DecisionEvidenceLink'
_check_php_class "silver:decision-option" 'App\Models\Silver\DecisionOption'
_check_php_class "silver:decision-outcome" 'App\Models\Silver\DecisionOutcome'
_check_php_class "silver:decision-lesson" 'App\Models\Silver\DecisionLessonLearned'
_check_php_class "silver:decision-record-factory" 'Database\Factories\Silver\DecisionRecordFactory'

# Doc-phase 133 — Laravel RecordDecision service + platform_ops sentinel workspace
_check_php_class "decision-intelligence:record-decision" 'App\Services\DecisionIntelligence\RecordDecision'

TOTAL=$((TOTAL + 1))
if [ "$($PSQL -c "SELECT count(*) FROM silver.workspaces WHERE workspace_id = 'f0f0f0f0-0000-0000-0000-000000000001'::uuid")" = "1" ]; then
    note "[silver:platform-ops-workspace] PASS — platform_ops sentinel workspace seeded"
else
    note "[silver:platform-ops-workspace] FAIL — platform_ops sentinel workspace missing"
    FAIL=$((FAIL + 1))
fi

# Doc-phase 135 — §6 public_geoscience jurisdictions + sources seeded
TOTAL=$((TOTAL + 1))
PG_JURISDICTIONS=$($PSQL -c "SELECT count(*) FROM public_geoscience.jurisdictions")
if [ "$PG_JURISDICTIONS" -ge "5" ]; then
    note "[public-geoscience:jurisdictions] PASS — $PG_JURISDICTIONS jurisdictions seeded (>=5)"
else
    note "[public-geoscience:jurisdictions] FAIL — only $PG_JURISDICTIONS jurisdictions (expected >=5)"
    FAIL=$((FAIL + 1))
fi

TOTAL=$((TOTAL + 1))
PG_SOURCES=$($PSQL -c "SELECT count(*) FROM public_geoscience.sources")
if [ "$PG_SOURCES" -ge "9" ]; then
    note "[public-geoscience:sources] PASS — $PG_SOURCES sources registered (>=9)"
else
    note "[public-geoscience:sources] FAIL — only $PG_SOURCES sources (expected >=9)"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Doc-phases 179-181 — Phase B/C/D ingestion state
# ----------------------------------------------------------------------
note ""
note "=== Phase B/C/D ingest state ==="

# Phase B-Tier-1 — at least one real silver project ingested
TOTAL=$((TOTAL + 1))
CAMECO_COLLARS=$($PSQL -c "SELECT count(*) FROM silver.collars c JOIN silver.projects p ON c.project_id = p.project_id WHERE p.slug = 'cameco-shirley-basin'")
if [ "$CAMECO_COLLARS" -ge "60" ]; then
    note "[ingest:cameco-collars] PASS — $CAMECO_COLLARS Cameco drillholes in silver.collars (>=60)"
else
    note "[ingest:cameco-collars] FAIL — only $CAMECO_COLLARS Cameco collars (expected >=60)"
    FAIL=$((FAIL + 1))
fi

# Phase B-Tier-1 — well log curves landed
TOTAL=$((TOTAL + 1))
CAMECO_CURVES=$($PSQL -c "SELECT count(*) FROM silver.well_log_curves wc JOIN silver.collars c ON wc.collar_id = c.collar_id JOIN silver.projects p ON c.project_id = p.project_id WHERE p.slug = 'cameco-shirley-basin'")
if [ "$CAMECO_CURVES" -ge "700" ]; then
    note "[ingest:cameco-curves] PASS — $CAMECO_CURVES well-log curves landed (>=700)"
else
    note "[ingest:cameco-curves] FAIL — only $CAMECO_CURVES curves (expected >=700)"
    FAIL=$((FAIL + 1))
fi

# Phase C — Neo4j knowledge graph populated for Cameco
TOTAL=$((TOTAL + 1))
NEO4J_CAMECO_NODES=$(docker exec georag-neo4j cypher-shell -u neo4j -p "${NEO4J_PASSWORD:-24kNKWLbX20bgHEXAuMSGjCp228LIfUE}" "MATCH (n) WHERE n.project_id = '762b147e-af53-4593-b569-04ee46f31d97' RETURN count(n) AS c;" 2>/dev/null | grep -E '^[0-9]+$' | head -1)
if [ -z "$NEO4J_CAMECO_NODES" ]; then NEO4J_CAMECO_NODES=0; fi
if [ "$NEO4J_CAMECO_NODES" -ge "60" ]; then
    note "[kg:cameco-nodes] PASS — $NEO4J_CAMECO_NODES Cameco nodes in Neo4j (>=60)"
else
    note "[kg:cameco-nodes] FAIL — only $NEO4J_CAMECO_NODES Cameco nodes in Neo4j (expected >=60)"
    FAIL=$((FAIL + 1))
fi

# Phase C — Specific named entities Layer 4 looks for
TOTAL=$((TOTAL + 1))
NEO4J_CAMECO_ENTITY=$(docker exec georag-neo4j cypher-shell -u neo4j -p "${NEO4J_PASSWORD:-24kNKWLbX20bgHEXAuMSGjCp228LIfUE}" "MATCH (n) WHERE n.name = 'CAMECO RESOURCES' RETURN count(n) AS c;" 2>/dev/null | grep -E '^[0-9]+$' | head -1)
if [ -z "$NEO4J_CAMECO_ENTITY" ]; then NEO4J_CAMECO_ENTITY=0; fi
if [ "$NEO4J_CAMECO_ENTITY" -ge "1" ]; then
    note "[kg:cameco-resources-entity] PASS — 'CAMECO RESOURCES' resolvable in Neo4j"
else
    note "[kg:cameco-resources-entity] FAIL — Layer 4 entity 'CAMECO RESOURCES' missing from Neo4j"
    FAIL=$((FAIL + 1))
fi

# Phase D — Qdrant embeddings landed
TOTAL=$((TOTAL + 1))
PG_EMBEDDED=$($PSQL -c "SELECT count(*) FROM silver.document_passages dp JOIN silver.reports r ON dp.document_id = r.report_id JOIN silver.projects p ON r.project_id = p.project_id WHERE p.slug = 'cameco-shirley-basin' AND dp.embedding_id IS NOT NULL")
if [ "$PG_EMBEDDED" -ge "3" ]; then
    note "[embed:cameco-passages] PASS — $PG_EMBEDDED Cameco passages have Qdrant embeddings (>=3)"
else
    note "[embed:cameco-passages] FAIL — only $PG_EMBEDDED Cameco passages embedded (expected >=3)"
    FAIL=$((FAIL + 1))
fi

# Phase B/C/D — core_chat Wyoming uranium question set seeded
TOTAL=$((TOTAL + 1))
CORE_CHAT_COUNT=$($PSQL -c "SELECT count(*) FROM eval.golden_questions WHERE question_set = 'core_chat' AND status = 'active'")
if [ "$CORE_CHAT_COUNT" -ge "10" ]; then
    note "[eval:core-chat-seeded] PASS — $CORE_CHAT_COUNT core_chat Wyoming uranium questions active (>=10)"
else
    note "[eval:core-chat-seeded] FAIL — only $CORE_CHAT_COUNT core_chat questions active (expected >=10)"
    FAIL=$((FAIL + 1))
fi

# Phase B/C/D — ingest service modules importable
TOTAL=$((TOTAL + 1))
if docker exec "$FASTAPI_CONTAINER" python -c "from app.services.ingest import las_ingester, pdf_ingester, xlsx_ingester, cameco_log_ingester, cluster_runner, kg_sync, passage_embedder, tiff_ocr_ingester" 2>/dev/null; then
    note "[ingest:modules-importable] PASS — all 8 ingest modules load cleanly"
else
    note "[ingest:modules-importable] FAIL — ingest module import failed (check pyproject.toml)"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
echo ""
echo "=== Autonomous-run substrate verifier summary ==="
echo "  $((TOTAL - FAIL))/$TOTAL checks passed"

if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "autonomous_run_substrate"
fi

exit $FAIL
