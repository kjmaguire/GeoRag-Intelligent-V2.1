#!/usr/bin/env bash
# Nightly cost-control shutdown sweep for the GeoRAG ECS cluster.
#
# Runs as an ECS task launched by the `georag-shutdown` EventBridge
# schedule; see deploy/aws/terraform/scheduler.tf (ADR-0022). Ported from
# the Azure Container Apps version, which is gone; what it learned the hard
# way is kept and what changed is called out below.
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
# CloudWatch alarm in deploy/aws/terraform/alerts.tf has something true to watch.
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
#
# ---------------------------------------------------------------------
# THE TIERS, IN REVERSE, SINCE 2026-09-29 (audit AWS-8)
# ---------------------------------------------------------------------
# This used to scale all ten services to 0 in one loop and stop RDS
# immediately after, on the theory that "nothing here depends on anything
# else". Shutdown order does matter, just less visibly than startup order:
# the hatchet worker got SIGTERM at the same instant as the engine, Qdrant
# and Redis it was writing to, and RDS began stopping while every task
# still held connections. A report ingesting at 17:00 died mid-upsert —
# silver rows and Qdrant points half-written, Parse pages billed and then
# billed again on the morning retry.
#
# So it is startup-sweep.sh's tiers backwards: Laravel first (no new work
# enters), then the workers and fastapi, then the stores and the engine, and
# RDS last. Each tier is waited on (`services-stable`, i.e. running = 0)
# before the next goes, which is what lets each task's stopTimeout
# (services.tf, 120 s on the worker, Horizon, fastapi, Qdrant and Redis)
# actually be spent draining. Expect the sweep to take a few minutes, not
# seconds. A tier that does not drain is reported and the sweep carries on:
# the cost saving still matters more than a clean stop, and the alarm tells
# a person which service to look at.
#
# RESIDUAL RISK, stated: 120 s is Fargate's maximum stopTimeout. Work that
# needs longer — a 300-page PDF at ~7.5 s/page — is still cut off and left to
# Hatchet's retry policy the next morning. Nothing blocks uploads near 17:00.
set -uo pipefail

CLUSTER="${SWEEP_CLUSTER:-georag}"
DB_INSTANCE="${SWEEP_DB_INSTANCE:-georag-pg}"

# startup-sweep.sh's TIER3, TIER2, TIER1 — keep the two files in step.
TIER3=(laravel-octane laravel-horizon laravel-reverb)
TIER2=(hatchet-worker fastapi martin)
TIER1=(redis qdrant hatchet sparse)
SERVICE_COUNT=$(( ${#TIER1[@]} + ${#TIER2[@]} + ${#TIER3[@]} ))

FAILURES=()

log()  { printf '%s\n' "$*" >&2; }
fail() { FAILURES+=("$1"); log "FAILED: $1"; }

stop_tier() {
  local label="$1"; shift
  log "--- ${label} ---"
  for svc in "$@"; do
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
  # One waiter call for the whole tier: the tasks drain in parallel, so the
  # tier costs the slowest stopTimeout, not the sum of them.
  if aws ecs wait services-stable --cluster "$CLUSTER" --services "$@" 2>/dev/null; then
    log "${label}: drained"
  else
    fail "${label} did not drain to 0 running tasks"
  fi
}

# --- ECS services -----------------------------------------------------
log "--- scaling ${SERVICE_COUNT} services to desired-count 0, one tier at a time ---"
stop_tier "tier 3: Laravel (stops new work entering)" "${TIER3[@]}"
stop_tier "tier 2: hatchet worker, fastapi, martin (drain in-flight work)" "${TIER2[@]}"
stop_tier "tier 1: redis, qdrant, hatchet engine, sparse (flush and stop)" "${TIER1[@]}"

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

# Services, plus the database.
TOTAL=$(( SERVICE_COUNT + 1 ))

if [ ${#FAILURES[@]} -eq 0 ]; then
  log "shutdown sweep complete: ${TOTAL}/${TOTAL} actions succeeded"
  exit 0
fi

log "shutdown sweep INCOMPLETE: ${#FAILURES[@]} of ${TOTAL} actions failed"
for f in "${FAILURES[@]}"; do
  log "  - $f"
done
exit 1
