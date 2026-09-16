#!/usr/bin/env bash
# Nightly cost-control shutdown sweep for the GeoRAG ECS cluster.
#
# Runs as an ECS task launched by the `georag-shutdown` EventBridge
# schedule; see deploy/aws/scheduler/schedules.tf. Ported from
# deploy/azure/containerapps/scripts/shutdown-sweep.sh on 2026-09-08
# (ADR-0022). Everything the Azure version learned the hard way is kept;
# what changed is called out below.
#
# ---------------------------------------------------------------------
# WHAT CHANGED FROM THE AZURE VERSION
# ---------------------------------------------------------------------
# 1. THE DST GUARD IS GONE. Container Apps Jobs schedule in UTC only, so
#    each sweep fired at BOTH candidate hours and an in-script guard
#    exited 0 on the wrong one — and scripts/check_scheduler_job_parity.py
#    existed partly to keep the cron and the guard agreeing. EventBridge
#    Scheduler takes a timezone, so there is one schedule, one fire, and
#    no guard. If you are tempted to reintroduce a double fire for any
#    reason: that guard was subtly wrong for two days a year until
#    2026-08-21, because it compared against midnight UTC rather than the
#    real transition instant. A double fire needs a guard, and a guard is
#    a second place for the schedule to be wrong.
#
# 2. SCALING TO ZERO ACTUALLY WORKS. On Container Apps `--min-replicas 0`
#    is a floor, not an off switch: an app with only the implicit HTTP
#    scale rule scaled in, and everything with TCP ingress or a busy
#    worker loop did not. hatchet-worker-cc — 4 vCPU / 8 GiB, the largest
#    single line item — never stopped, because it has 29 cron expressions
#    and two of them fire every minute. ECS `desired-count 0` stops tasks
#    outright, so this sweep genuinely stops all ten services, worker
#    included. That was the biggest single cost item the Azure sweep could
#    not touch.
#
# 3. THERE IS NO MODEL TIER TO SWEEP. Until 2026-09-15 this script also
#    deleted Bedrock Marketplace endpoints for Command A+ and Cohere Parse
#    5, because a Marketplace endpoint bills for as long as it exists and
#    the only way not to pay is to delete it. ADR-0023 moved both onto
#    Cohere's own API, where billing is per token and per page: nothing
#    accrues overnight, so nothing needs deleting.
#
#    The code is REMOVED rather than left inert behind an unset variable.
#    The scheduler role no longer holds sagemaker:DeleteEndpoint, so a
#    dormant branch someone re-enabled would fail with AccessDenied at 2am
#    — a trap dressed as a feature flag. Embeddings and reranking are still
#    Bedrock calls, but they are serverless and cost nothing at rest.
#
# ---------------------------------------------------------------------
# WHY THIS IS NOT `set -e` AND NOT `|| echo "skip ..."`
# ---------------------------------------------------------------------
# Unchanged from Azure, and still the right answer. The original body ran
# `set -euo pipefail` and then masked every command with
# `|| echo "skip $app"`. That combination is the worst of both: the mask
# defeats -e, so a failed action never stops the sweep, and it never
# reaches the exit status either, so the run reports success with nothing
# amiss in the log except the word "skip".
#
# Removing the masks is not the fix. Under `set -e` a single failed
# update would abort the sweep and leave the remaining services running —
# and on the startup side it would leave the whole platform down for the
# working day. The actions are independent; one failing is not a reason
# to skip the others.
#
# So: no -e, every action attempted, failures collected, and a non-zero
# exit at the end. The task's exit code then means what it says, and the
# CloudWatch alarm in deploy/aws/alerts/ has something true to watch.
#
# ---------------------------------------------------------------------
# WHY PROGRESS GOES TO STDERR
# ---------------------------------------------------------------------
# This is a property of the container runtime, not of Azure, so it
# survives the move unchanged. bash's stdout is a pipe here, so it is
# block-buffered and only flushes when the process exits. Measured on the
# 2026-08-20 Azure run: every stderr line appeared at its real time, while
# two stdout lines carried the timestamp of the instant the container was
# terminated, two and a half minutes after the events they described.
#
# That is also how a truncated sweep managed to print "shutdown sweep
# complete": the line was already in the buffer, and the flush on teardown
# made a killed run look finished. Anything this script says about its own
# progress goes to stderr so it is emitted when it happens and survives
# the container being killed mid-sweep.
#
# ---------------------------------------------------------------------
# WHY RESULTS ARE CHECKED BY STATE, NOT BY EXIT CODE
# ---------------------------------------------------------------------
# Also unchanged, and the AWS CLI has the same property the Azure one did:
# `aws rds stop-db-instance` fails both when the stop genuinely failed and
# when the instance is already stopped, and a long-running operation can
# be interrupted after the API has already accepted it. Matching on error
# strings means guessing error codes. Reading the state afterwards does
# not — and the converse matters more: an exit code of 0 with the instance
# still `available` is a failure, and only a state read catches it.
set -uo pipefail

CLUSTER="${SWEEP_CLUSTER:-georag}"
DB_INSTANCE="${SWEEP_DB_INSTANCE:-georag-pg}"

# Every service the sweep stops. Order is irrelevant — nothing here
# depends on anything else, unlike the startup tiers.
SERVICES=(
  redis
  qdrant
  sparse
  hatchet-worker
  hatchet
  fastapi
  martin
  laravel-octane
  laravel-horizon
  laravel-reverb
)

FAILURES=()

log()  { printf '%s\n' "$*" >&2; }
fail() { FAILURES+=("$1"); log "FAILED: $1"; }

# --- ECS services -----------------------------------------------------
log "--- scaling ${#SERVICES[@]} services to desired-count 0 ---"
for svc in "${SERVICES[@]}"; do
  # --output text --query: `aws ecs update-service` otherwise dumps the
  # entire service description, task definition and all. The two Azure
  # scheduler jobs emitted 13,197 console lines over 2026-08-20..21 that
  # way, which is what buries the handful of lines that matter.
  if aws ecs update-service \
       --cluster "$CLUSTER" --service "$svc" --desired-count 0 \
       --query 'service.serviceName' --output text >/dev/null; then
    log "$svc: desired-count 0"
  else
    fail "desired-count 0 on $svc"
  fi
done

# --- RDS --------------------------------------------------------------
log "--- stopping ${DB_INSTANCE} ---"
stop_rc=0
aws rds stop-db-instance --db-instance-identifier "$DB_INSTANCE" \
  --query 'DBInstance.DBInstanceIdentifier' --output text >/dev/null 2>&1 || stop_rc=$?

db_state=$(aws rds describe-db-instances --db-instance-identifier "$DB_INSTANCE" \
             --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null || echo "")
case "$db_state" in
  stopped|stopping)
    if [ "$stop_rc" -ne 0 ]; then
      log "${DB_INSTANCE}: stop command exited ${stop_rc} but instance is ${db_state} -- treating as success"
    else
      log "${DB_INSTANCE}: ${db_state}"
    fi
    ;;
  "")
    fail "could not read ${DB_INSTANCE} state after stop (stop exited ${stop_rc})"
    ;;
  *)
    fail "${DB_INSTANCE} is still ${db_state} after stop (stop exited ${stop_rc})"
    ;;
esac

# Services, plus the database. The endpoint term is gone with ADR-0023.
TOTAL=$(( ${#SERVICES[@]} + 1 ))

if [ ${#FAILURES[@]} -eq 0 ]; then
  log "shutdown sweep complete: ${TOTAL}/${TOTAL} actions succeeded"
  exit 0
fi

log "shutdown sweep INCOMPLETE: ${#FAILURES[@]} of ${TOTAL} actions failed"
for f in "${FAILURES[@]}"; do
  log "  - $f"
done
exit 1
