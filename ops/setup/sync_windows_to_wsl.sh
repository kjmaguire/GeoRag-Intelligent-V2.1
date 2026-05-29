#!/usr/bin/env bash
# DEPRECATED 2026-05-23. The canonical georag repo is now the Windows
# clone at C:\Users\GeoRAG\Herd\georag. The WSL clone at
# /home/georag/projects/georag was supposed to be canonical (2026-05-19
# decision) but by 2026-05-21 it had degenerated to a ~32 KB skeleton
# while active work continued on the Windows clone — see the auto-memory
# entries [[wsl-canonical-decision]] (with its 2026-05-21 reversal note)
# and [[local-environment]].
#
# This script is preserved for:
#   * one-off pushes from Windows to a temporary WSL test environment
#   * historical reference for the original Windows-to-WSL bridge
#
# It refuses to run by default. Pass --force to actually sync.
#
# The 'right' next step (Task O of the 2026-05-23 autonomous run) is to
# archive the WSL clone entirely. That requires direct WSL shell access
# Kyle hasn't authorised yet; the relevant procedure is documented inline
# below.
set -euo pipefail

if [ "${1:-}" != "--force" ]; then
  cat >&2 <<'EOF'
sync_windows_to_wsl.sh is DEPRECATED.

The Windows clone (C:\Users\GeoRAG\Herd\georag) is canonical as of
2026-05-23. The WSL clone is a stale skeleton with diverged HEAD; do
NOT push current work there — you'll create the same drift bug this
deprecation closes.

If you genuinely need to refresh the WSL clone (e.g. for testing in
a Linux-native shell), re-run with --force. Otherwise, retire it via:

  # From PowerShell, inside WSL Ubuntu shell:
  cd ~/projects
  mv georag georag-retired-$(date +%Y%m%d)
  # Optional: tar + delete after a week of confirming nothing breaks.

EOF
  exit 64  # EX_USAGE
fi

WIN_REPO='/mnt/c/Users/GeoRAG/Herd/georag'
WSL_REPO='/home/georag/projects/georag'

if [ ! -d "$WIN_REPO" ]; then
  echo "Windows repo not reachable at $WIN_REPO" >&2
  exit 1
fi
if [ ! -d "$WSL_REPO" ]; then
  echo "WSL repo not reachable at $WSL_REPO" >&2
  exit 1
fi

# v1.13 — MoE telemetry sidecar [RETIRED 2026-05-10 by Ollama→vLLM cutover]
#   moe_telemetry_exporter.py + compose.moe-telemetry.yml moved to _deprecated/.
#   vLLM exposes its own Prometheus /metrics endpoint — sidecar no longer needed.

# v1.13 — Modelfiles [RETIRED 2026-05-10 by Ollama→vLLM cutover]
#   Three Modelfile.qwen* files moved to docker/_deprecated/ollama/.
#   Project now uses ELVISIO/Qwen3-30B-A3B-Instruct-2507-AWQ via vLLM.

# v1.13 — Validator (LLM-as-judge addition) [RETIRED 2026-05-10]
#   qwen_moe_validator.py moved to ops/_deprecated/validation/.
#   Phase 2 of vLLM cutover will add a vLLM-native validator that lifts the
#   geological prompts + scoring rubric from the deprecated file.

# v1.14 — Postgres tuning init
cp -v "$WIN_REPO/docker/postgresql/init/Z_activate_threadripper_tuning.sql" \
      "$WSL_REPO/docker/postgresql/init/"

# v1.14 — Prometheus rules + jobs (created but not yet wired into running stack)
mkdir -p "$WSL_REPO/docker/prometheus/jobs"
cp -v "$WIN_REPO/docker/prometheus/jobs/moe-telemetry.yml"   "$WSL_REPO/docker/prometheus/jobs/"
# moe-alerts.yml [RETIRED 2026-05-10] — moved to docker/_deprecated/prometheus/rules/.

# v1.15 — vLLM cutover (2026-05-08/09). Code, configs, tests, ops assets.
# Phase 1 (pre-prep) + Phase 2 (orchestrator backend-conditional, deprecations).
# FastAPI source
cp -v "$WIN_REPO/src/fastapi/app/config.py"                      "$WSL_REPO/src/fastapi/app/config.py"
cp -v "$WIN_REPO/src/fastapi/app/agent/orchestrator.py"          "$WSL_REPO/src/fastapi/app/agent/orchestrator.py"
cp -v "$WIN_REPO/src/fastapi/app/agent/model_routing.py"         "$WSL_REPO/src/fastapi/app/agent/model_routing.py"
cp -v "$WIN_REPO/src/fastapi/tests/test_vllm_payload_shape.py"   "$WSL_REPO/src/fastapi/tests/test_vllm_payload_shape.py"
# Laravel + env
cp -v "$WIN_REPO/config/ai.php"                                  "$WSL_REPO/config/ai.php"
cp -v "$WIN_REPO/.env.example"                                   "$WSL_REPO/.env.example"
cp -v "$WIN_REPO/.env.production.example"                        "$WSL_REPO/.env.production.example"
cp -v "$WIN_REPO/CLAUDE.md"                                      "$WSL_REPO/CLAUDE.md"
# Docs
cp -v "$WIN_REPO/docs/RUNBOOK.md"                                "$WSL_REPO/docs/RUNBOOK.md"
cp -v "$WIN_REPO/docs/model_migration.md"                        "$WSL_REPO/docs/model_migration.md"
cp -v "$WIN_REPO/ops/baselines/capacity-planning.md"             "$WSL_REPO/ops/baselines/capacity-planning.md"
cp -v "$WIN_REPO/ops/runbooks/cold-start.md"                     "$WSL_REPO/ops/runbooks/cold-start.md"
# Deprecation tree (mirrors the Windows-side _deprecated layout). Stop re-syncing
# the live paths — the originals were moved to _deprecated/ on 2026-05-10 and the
# live paths no longer exist on the Windows side.
mkdir -p "$WSL_REPO/docker/_deprecated/ollama" \
         "$WSL_REPO/docker/_deprecated/prometheus/rules" \
         "$WSL_REPO/ops/_deprecated/observability" \
         "$WSL_REPO/ops/_deprecated/validation"
cp -v "$WIN_REPO/docker/_deprecated/README.md"                                "$WSL_REPO/docker/_deprecated/"
cp -v "$WIN_REPO/docker/_deprecated/compose.moe-telemetry.yml"                "$WSL_REPO/docker/_deprecated/"
cp -v "$WIN_REPO/docker/_deprecated/ollama/Modelfile.qwen2.5-14b-georag"      "$WSL_REPO/docker/_deprecated/ollama/"
cp -v "$WIN_REPO/docker/_deprecated/ollama/Modelfile.qwen3-30b-a3b-georag"    "$WSL_REPO/docker/_deprecated/ollama/"
cp -v "$WIN_REPO/docker/_deprecated/ollama/Modelfile.qwen3-30b-a3b-q5km"      "$WSL_REPO/docker/_deprecated/ollama/"
cp -v "$WIN_REPO/docker/_deprecated/prometheus/rules/moe-alerts.yml"          "$WSL_REPO/docker/_deprecated/prometheus/rules/"
cp -v "$WIN_REPO/ops/_deprecated/README.md"                                   "$WSL_REPO/ops/_deprecated/"
cp -v "$WIN_REPO/ops/_deprecated/observability/moe_telemetry_exporter.py"     "$WSL_REPO/ops/_deprecated/observability/"
cp -v "$WIN_REPO/ops/_deprecated/validation/qwen_moe_validator.py"            "$WSL_REPO/ops/_deprecated/validation/"
# New vLLM ops assets
cp -v "$WIN_REPO/docker/compose.vllm.yml"                        "$WSL_REPO/docker/compose.vllm.yml"
cp -v "$WIN_REPO/docker/prometheus/jobs/vllm.yml"                "$WSL_REPO/docker/prometheus/jobs/vllm.yml"
cp -v "$WIN_REPO/docker/prometheus/rules/vllm-alerts.yml"        "$WSL_REPO/docker/prometheus/rules/vllm-alerts.yml"
cp -v "$WIN_REPO/ops/validation/vllm_a4500_smoke.sh"             "$WSL_REPO/ops/validation/vllm_a4500_smoke.sh"
chmod +x "$WSL_REPO/ops/validation/vllm_a4500_smoke.sh"

# v1.16 — Phase 0 step 1 (2026-05-09). Custom PG image, init SQL, SeaweedFS
# entrypoint, verify scripts. docker-compose.yml stays WSL-only (canonical).
mkdir -p "$WSL_REPO/docker/postgresql" \
         "$WSL_REPO/docker/postgresql/init" \
         "$WSL_REPO/docker/seaweedfs" \
         "$WSL_REPO/scripts"
cp -v "$WIN_REPO/docker/postgresql/Dockerfile"                                "$WSL_REPO/docker/postgresql/Dockerfile"
cp -v "$WIN_REPO/docker/postgresql/init/10-phase0-extensions-and-schemas.sql" "$WSL_REPO/docker/postgresql/init/10-phase0-extensions-and-schemas.sql"
cp -v "$WIN_REPO/docker/postgresql/init/20-hatchet-database.sql"              "$WSL_REPO/docker/postgresql/init/20-hatchet-database.sql"
cp -v "$WIN_REPO/docker/seaweedfs/entrypoint.sh"                              "$WSL_REPO/docker/seaweedfs/entrypoint.sh"
cp -v "$WIN_REPO/scripts/phase0_apply_extensions.sh"                          "$WSL_REPO/scripts/phase0_apply_extensions.sh"
cp -v "$WIN_REPO/scripts/phase0_step1_verify.sh"                              "$WSL_REPO/scripts/phase0_step1_verify.sh"
chmod +x "$WSL_REPO/scripts/phase0_apply_extensions.sh" \
         "$WSL_REPO/scripts/phase0_step1_verify.sh" \
         "$WSL_REPO/docker/seaweedfs/entrypoint.sh"

# v1.17 — Phase 0 step 2 (2026-05-09). 24-table schema deployment + RLS + hash chain.
mkdir -p "$WSL_REPO/database/raw/phase0"
cp -v "$WIN_REPO/database/raw/phase0/"*.sql                       "$WSL_REPO/database/raw/phase0/"
cp -v "$WIN_REPO/scripts/phase0_step2_apply.sh"                   "$WSL_REPO/scripts/phase0_step2_apply.sh"
cp -v "$WIN_REPO/scripts/phase0_step2_verify.sh"                  "$WSL_REPO/scripts/phase0_step2_verify.sh"
chmod +x "$WSL_REPO/scripts/phase0_step2_apply.sh" \
         "$WSL_REPO/scripts/phase0_step2_verify.sh"

# v1.18 — Phase 0 step 3 (2026-05-09). OTel collector + Tempo + scrape jobs.
mkdir -p "$WSL_REPO/docker/otel-collector" \
         "$WSL_REPO/docker/tempo"
cp -v "$WIN_REPO/docker/otel-collector/otel-collector-config.yaml" "$WSL_REPO/docker/otel-collector/otel-collector-config.yaml"
cp -v "$WIN_REPO/docker/tempo/tempo-config.yaml"                   "$WSL_REPO/docker/tempo/tempo-config.yaml"
cp -v "$WIN_REPO/docker/prometheus/prometheus.yml"                 "$WSL_REPO/docker/prometheus/prometheus.yml"
cp -v "$WIN_REPO/scripts/emit_test_span.sh"                        "$WSL_REPO/scripts/emit_test_span.sh"
cp -v "$WIN_REPO/scripts/phase0_step3_verify.sh"                   "$WSL_REPO/scripts/phase0_step3_verify.sh"
chmod +x "$WSL_REPO/scripts/emit_test_span.sh" \
         "$WSL_REPO/scripts/phase0_step3_verify.sh"

# v1.19 — Phase 0 step 4 (2026-05-09). Audit emitter (Python + PHP),
# verifier function, hash recipe doc, smoke test.
mkdir -p "$WSL_REPO/src/fastapi/app/audit" \
         "$WSL_REPO/app/Services/Audit"
cp -v "$WIN_REPO/src/fastapi/app/audit/__init__.py"               "$WSL_REPO/src/fastapi/app/audit/__init__.py"
cp -v "$WIN_REPO/app/Services/Audit/AuditEmitter.php"             "$WSL_REPO/app/Services/Audit/AuditEmitter.php"
cp -v "$WIN_REPO/database/raw/phase0/100-audit-verify-function.sql" "$WSL_REPO/database/raw/phase0/100-audit-verify-function.sql"
cp -v "$WIN_REPO/database/raw/phase0/90-audit-hash-chain-trigger.sql" "$WSL_REPO/database/raw/phase0/90-audit-hash-chain-trigger.sql"
cp -v "$WIN_REPO/database/raw/phase0/20-layer-b-audit-ledger.sql" "$WSL_REPO/database/raw/phase0/20-layer-b-audit-ledger.sql"
cp -v "$WIN_REPO/docs/audit_ledger_hash_recipe.md"                "$WSL_REPO/docs/audit_ledger_hash_recipe.md"
cp -v "$WIN_REPO/scripts/phase0_audit_outbox_smoke.sh"            "$WSL_REPO/scripts/phase0_audit_outbox_smoke.sh"
cp -v "$WIN_REPO/scripts/phase0_step4_verify.sh"                  "$WSL_REPO/scripts/phase0_step4_verify.sh"
chmod +x "$WSL_REPO/scripts/phase0_audit_outbox_smoke.sh" \
         "$WSL_REPO/scripts/phase0_step4_verify.sh"

# v1.20 — Phase 0 step 3 closing sub-task (2026-05-09): Workflow Run
# Dashboard skeleton (Laravel admin route + Inertia React page + basic
# feature test). Reads workflow.workflow_runs (the partman-monthly
# partitioned table from step 2) and links each trace_id to Tempo via
# config('services.tempo.url') (TEMPO_HOST_URL env). The verifier picks
# up a 6th check for the route. Edits to existing files (routes/web.php
# and config/services.php) are NOT mirrored from Win — those two have
# drifted in WSL since v1.15 and the WSL side is canonical for them; if
# the Win edits diverge, merge by hand rather than `cp`-overwriting.
mkdir -p "$WSL_REPO/app/Http/Controllers/Admin" \
         "$WSL_REPO/resources/js/Pages/Admin" \
         "$WSL_REPO/tests/Feature/Admin"
cp -v "$WIN_REPO/app/Http/Controllers/Admin/WorkflowRunController.php" \
      "$WSL_REPO/app/Http/Controllers/Admin/WorkflowRunController.php"
cp -v "$WIN_REPO/resources/js/Pages/Admin/WorkflowRuns.tsx" \
      "$WSL_REPO/resources/js/Pages/Admin/WorkflowRuns.tsx"
cp -v "$WIN_REPO/tests/Feature/Admin/WorkflowRunDashboardTest.php" \
      "$WSL_REPO/tests/Feature/Admin/WorkflowRunDashboardTest.php"
cp -v "$WIN_REPO/scripts/phase0_step3_verify.sh"                   "$WSL_REPO/scripts/phase0_step3_verify.sh"
chmod +x "$WSL_REPO/scripts/phase0_step3_verify.sh"

# v1.21 — Phase 0 step 4 close-out (2026-05-09): Hatchet workflows for the
# audit-ledger nightly verifier and the outbox dispatcher. New worker module
# under src/fastapi/app/hatchet_workflows/ + a worker compose service
# (georag-hatchet-worker, defined directly in the WSL-canonical
# docker-compose.yml). pyproject.toml gains hatchet-sdk + boto3 — note that
# the Win-side pyproject is older than WSL-side and therefore only carries
# the two new entries; absorbing the rest of the WSL drift is out of scope
# here.  phase0_step4_verify.sh expands from 5/5 to 7/7.
#
# Auth bootstrap (one-time, NOT idempotent):
#   docker exec georag-hatchet /hatchet-admin --config /config token create \
#       --name georag-worker \
#       --tenant-id "$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
#                       \"SELECT id FROM \\\"Tenant\\\" WHERE slug='default';\")"
# Paste the resulting JWT into HATCHET_CLIENT_TOKEN in the WSL .env
# (already done on 2026-05-09).
mkdir -p "$WSL_REPO/src/fastapi/app/hatchet_workflows"
cp -v "$WIN_REPO/src/fastapi/app/hatchet_workflows/__init__.py" \
      "$WSL_REPO/src/fastapi/app/hatchet_workflows/__init__.py"
cp -v "$WIN_REPO/src/fastapi/app/hatchet_workflows/audit_ledger_verify.py" \
      "$WSL_REPO/src/fastapi/app/hatchet_workflows/audit_ledger_verify.py"
cp -v "$WIN_REPO/src/fastapi/app/hatchet_workflows/outbox_dispatcher.py" \
      "$WSL_REPO/src/fastapi/app/hatchet_workflows/outbox_dispatcher.py"
cp -v "$WIN_REPO/src/fastapi/app/hatchet_workflows/worker.py" \
      "$WSL_REPO/src/fastapi/app/hatchet_workflows/worker.py"
cp -v "$WIN_REPO/scripts/phase0_step4_verify.sh"                   "$WSL_REPO/scripts/phase0_step4_verify.sh"
chmod +x "$WSL_REPO/scripts/phase0_step4_verify.sh"
# pyproject.toml — Win tree is OLDER than WSL (see file header).  Skipping
# the cp -v overwrite: the two new deps (hatchet-sdk + boto3) were applied
# directly to the WSL-canonical pyproject and the Win tree carries them as
# parallel adds.  When the trees are reconciled, copy the WSL pyproject.toml
# back into Win and delete this notice.
# docker-compose.yml — WSL-only canonical; the georag-hatchet-worker service
# block lives there and was NOT mirrored to Win.

# v1.22 — Phase 0 step 5.2 (2026-05-09): Agent Config admin surfaces under
# /admin/agent-config/{timeouts,prompts,pins,workspaces}. Laravel
# controllers + Form Requests + Inertia React pages + PHPUnit feature
# tests. The verifier picks up 4 new route-probe checks, going from
# 5/5 to 9/9. Same caveat as v1.20: routes/web.php has drifted in WSL
# since v1.15 — DO NOT cp-overwrite it. The route registration block
# this step adds (4 prefixed agent-config routes + the previously-
# skeleton workflow-runs route) must be merged by hand into the
# WSL-side routes/web.php. The RequiresPostgres trait was authored
# alongside (referenced by the pre-existing WorkflowRunDashboardTest
# but never landed) and IS safe to cp in fresh.
mkdir -p "$WSL_REPO/app/Http/Controllers/Admin/AgentConfig" \
         "$WSL_REPO/app/Http/Requests/Admin/AgentConfig" \
         "$WSL_REPO/resources/js/Pages/Admin/AgentConfig" \
         "$WSL_REPO/tests/Concerns" \
         "$WSL_REPO/tests/Feature/Admin/AgentConfig"
cp -v "$WIN_REPO/app/Http/Controllers/Admin/AgentConfig/TimeoutsController.php" \
      "$WSL_REPO/app/Http/Controllers/Admin/AgentConfig/TimeoutsController.php"
cp -v "$WIN_REPO/app/Http/Controllers/Admin/AgentConfig/PromptsController.php" \
      "$WSL_REPO/app/Http/Controllers/Admin/AgentConfig/PromptsController.php"
cp -v "$WIN_REPO/app/Http/Controllers/Admin/AgentConfig/PinsController.php" \
      "$WSL_REPO/app/Http/Controllers/Admin/AgentConfig/PinsController.php"
cp -v "$WIN_REPO/app/Http/Controllers/Admin/AgentConfig/WorkspacesController.php" \
      "$WSL_REPO/app/Http/Controllers/Admin/AgentConfig/WorkspacesController.php"
cp -v "$WIN_REPO/app/Http/Requests/Admin/AgentConfig/UpdateTimeoutRequest.php" \
      "$WSL_REPO/app/Http/Requests/Admin/AgentConfig/UpdateTimeoutRequest.php"
cp -v "$WIN_REPO/app/Http/Requests/Admin/AgentConfig/PromotePromptRequest.php" \
      "$WSL_REPO/app/Http/Requests/Admin/AgentConfig/PromotePromptRequest.php"
cp -v "$WIN_REPO/app/Http/Requests/Admin/AgentConfig/UpdatePinRequest.php" \
      "$WSL_REPO/app/Http/Requests/Admin/AgentConfig/UpdatePinRequest.php"
cp -v "$WIN_REPO/app/Http/Requests/Admin/AgentConfig/UpdateWorkspaceConfigRequest.php" \
      "$WSL_REPO/app/Http/Requests/Admin/AgentConfig/UpdateWorkspaceConfigRequest.php"
cp -v "$WIN_REPO/resources/js/Pages/Admin/AgentConfig/Timeouts.tsx" \
      "$WSL_REPO/resources/js/Pages/Admin/AgentConfig/Timeouts.tsx"
cp -v "$WIN_REPO/resources/js/Pages/Admin/AgentConfig/Prompts.tsx" \
      "$WSL_REPO/resources/js/Pages/Admin/AgentConfig/Prompts.tsx"
cp -v "$WIN_REPO/resources/js/Pages/Admin/AgentConfig/Pins.tsx" \
      "$WSL_REPO/resources/js/Pages/Admin/AgentConfig/Pins.tsx"
cp -v "$WIN_REPO/resources/js/Pages/Admin/AgentConfig/Workspaces.tsx" \
      "$WSL_REPO/resources/js/Pages/Admin/AgentConfig/Workspaces.tsx"
cp -v "$WIN_REPO/tests/Concerns/RequiresPostgres.php" \
      "$WSL_REPO/tests/Concerns/RequiresPostgres.php"
cp -v "$WIN_REPO/tests/Feature/Admin/AgentConfig/TimeoutsTest.php" \
      "$WSL_REPO/tests/Feature/Admin/AgentConfig/TimeoutsTest.php"
cp -v "$WIN_REPO/tests/Feature/Admin/AgentConfig/PromptsTest.php" \
      "$WSL_REPO/tests/Feature/Admin/AgentConfig/PromptsTest.php"
cp -v "$WIN_REPO/tests/Feature/Admin/AgentConfig/PinsTest.php" \
      "$WSL_REPO/tests/Feature/Admin/AgentConfig/PinsTest.php"
cp -v "$WIN_REPO/tests/Feature/Admin/AgentConfig/WorkspacesTest.php" \
      "$WSL_REPO/tests/Feature/Admin/AgentConfig/WorkspacesTest.php"
cp -v "$WIN_REPO/scripts/phase0_step5_verify.sh"                   "$WSL_REPO/scripts/phase0_step5_verify.sh"
chmod +x "$WSL_REPO/scripts/phase0_step5_verify.sh"


# v1.23 — Phase 0 step 6 — remaining 6 Phase 0 agents (2026-05-09).
# Closes step 6 from 4/11 to 10/11 (the 11th, GPU/VRAM Health, is
# Prometheus rules — landed in v1.21). New agents land in
# src/fastapi/app/agents/phase0/ and a new on-demand router lives at
# src/fastapi/app/routers/phase0_ops.py. The Step 6 supplement migration
# adds silver.support_packets + prompt_versions/pin seeds for the two
# LLM-calling agents. pyproject.toml gains aioboto3>=13.0.
#
# main.py edit registers the phase0_ops router — same caveat as v1.20 +
# v1.22: main.py has drifted in WSL since the earlier sync rounds; merge
# the include_router(phase0_ops_router…) block + the corresponding
# import by hand on the WSL side rather than `cp`-overwriting.
mkdir -p "$WSL_REPO/src/fastapi/app/agents/phase0" \
         "$WSL_REPO/src/fastapi/app/routers" \
         "$WSL_REPO/database/raw/phase0"
cp -v "$WIN_REPO/src/fastapi/app/agents/phase0/__init__.py" \
      "$WSL_REPO/src/fastapi/app/agents/phase0/__init__.py"
cp -v "$WIN_REPO/src/fastapi/app/agents/phase0/storage_tiering.py" \
      "$WSL_REPO/src/fastapi/app/agents/phase0/storage_tiering.py"
cp -v "$WIN_REPO/src/fastapi/app/agents/phase0/model_upgrade_watch.py" \
      "$WSL_REPO/src/fastapi/app/agents/phase0/model_upgrade_watch.py"
cp -v "$WIN_REPO/src/fastapi/app/agents/phase0/vllm_security_check.py" \
      "$WSL_REPO/src/fastapi/app/agents/phase0/vllm_security_check.py"
cp -v "$WIN_REPO/src/fastapi/app/agents/phase0/model_cost_summary.py" \
      "$WSL_REPO/src/fastapi/app/agents/phase0/model_cost_summary.py"
cp -v "$WIN_REPO/src/fastapi/app/agents/phase0/llm_incident_diagnosis.py" \
      "$WSL_REPO/src/fastapi/app/agents/phase0/llm_incident_diagnosis.py"
cp -v "$WIN_REPO/src/fastapi/app/agents/phase0/support_packet.py" \
      "$WSL_REPO/src/fastapi/app/agents/phase0/support_packet.py"
cp -v "$WIN_REPO/src/fastapi/app/routers/phase0_ops.py" \
      "$WSL_REPO/src/fastapi/app/routers/phase0_ops.py"
# pyproject.toml — sync to pick up aioboto3 dep. Per the v1.15 note in
# project_vllm_migration memory, the WSL-canonical pyproject.toml has
# additional deps (paddleocr, docling, …) the Win tree lacks; if the WSL
# tree is canonical for those, merge by hand instead of cp-overwriting.
cp -v "$WIN_REPO/src/fastapi/pyproject.toml" \
      "$WSL_REPO/src/fastapi/pyproject.toml"
cp -v "$WIN_REPO/database/raw/phase0/120-phase0-step6-support-packets.sql" \
      "$WSL_REPO/database/raw/phase0/120-phase0-step6-support-packets.sql"
cp -v "$WIN_REPO/scripts/phase0_step6_verify.sh" \
      "$WSL_REPO/scripts/phase0_step6_verify.sh"
cp -v "$WIN_REPO/scripts/_phase0_step6_smoke.py" \
      "$WSL_REPO/scripts/_phase0_step6_smoke.py"
chmod +x "$WSL_REPO/scripts/phase0_step6_verify.sh"


echo
echo "=== Sync complete ==="
echo "Files in WSL: ops/observability/, docker/ollama/, docker/postgresql/init/, docker/prometheus/, src/fastapi/, config/, docs/, .env.*"
