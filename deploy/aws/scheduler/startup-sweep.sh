#!/usr/bin/env bash
# Morning startup sweep for the GeoRAG ECS cluster.
#
# Runs as an ECS task launched by the `georag-startup` EventBridge
# schedule; see deploy/aws/scheduler/schedules.tf. Ported from
# deploy/azure/containerapps/scripts/startup-sweep.sh on 2026-09-08
# (ADR-0022). Read shutdown-sweep.sh's header first — it carries the
# shared contract (no `set -e`, progress to stderr, state checks not exit
# codes, and why the DST guard is gone).
#
# ---------------------------------------------------------------------
# THE TIERS ARE NOT DECORATION
# ---------------------------------------------------------------------
# Unchanged from Azure. Starting everything at once works until it does
# not: fastapi's lifespan opens a Postgres pool and checks the live Qdrant
# dense dimension, the Hatchet worker cannot register workflows without
# the engine, and Laravel's chat bridge posts to fastapi on the first
# request. Each tier waits for the one below to be genuinely running
# before the next is scaled up, so a cold morning does not produce a
# stampede of crash-looping tasks and a pile of alarms.
#
# ---------------------------------------------------------------------
# THE BEDROCK ENDPOINT STEP IS THE ONE THAT CAN LEAVE THE PLATFORM DEAD
# ---------------------------------------------------------------------
# The sharpest edge this sweep carried is GONE as of 2026-09-15 (ADR-0023),
# and it is worth recording what it was. Command A+ and Cohere Parse 5 ran
# on SageMaker-managed endpoints that bill while they exist, so the
# shutdown sweep deleted them and this one recreated them from retained
# configs — minutes to reach InService, able to fail outright, and a failed
# recreate was no chat and no OCR at all with NO invocation metric to alarm
# on, because there were no invocations to fail. That is why this sweep
# waited for InService and emitted `BEDROCK_ENDPOINT_NOT_INSERVICE`.
#
# Both models moved to Cohere's own API, which has no endpoint to stand up
# and bills per token and per page. There is nothing to recreate, nothing
# to wait for, and nothing that can fail silently at 6am. The whole block
# is deleted rather than gated on an unset variable: the scheduler role no
# longer holds sagemaker:CreateEndpoint, so a dormant branch someone
# re-enabled would fail with AccessDenied.
#
# Embeddings and reranking are still Bedrock, and always were serverless —
# nothing to cycle, nothing accruing overnight.
set -uo pipefail

CLUSTER="${SWEEP_CLUSTER:-georag}"
DB_INSTANCE="${SWEEP_DB_INSTANCE:-georag-pg}"

TIER1=(redis qdrant hatchet sparse)
TIER2=(hatchet-worker fastapi martin)
TIER3=(laravel-octane laravel-horizon laravel-reverb)
SERVICE_COUNT=$(( ${#TIER1[@]} + ${#TIER2[@]} + ${#TIER3[@]} ))

# The two ALB-reachable services run two tasks; everything else runs one.
# The floor matters and the ceiling does not: at desired 1 every deploy and
# task replacement is a user-visible outage on a public service. ADR-0022
# §3 carries the cost reasoning for Octane, and the evidence that answered
# the Azure-era objection to it: max_connections 429 against a 24h peak of
# 99, Octane opening PDO connections lazily per worker, and session, cache
# and queue all on Redis. For Reverb the outage is every open WebSocket,
# which on this platform is every in-flight answer stream.
#
# THIS TABLE IS A SECOND PLACE THE COUNT LIVES. Terraform's local.services
# in deploy/aws/terraform/main.tf is the other, and the sweep overwrites
# Terraform's value every morning — so a count changed in one place and
# not the other silently reverts overnight. Change both.
#
# laravel-reverb at 2 is only correct while REVERB_SCALING_ENABLED is
# true (config.tf, reverb_server_environment). Without the Redis pub/sub
# backplane a second task drops roughly half of every query's frames.
declare -A DESIRED=( [laravel-octane]=2 [laravel-reverb]=2 )

# Floored at 1. A zero interval makes a wait loop spin without ever
# advancing its own clock, which is an infinite loop holding an ECS task
# open until the platform's own timeout kills it — found by the test
# harness passing 0 to make the cases fast. Kept after the endpoint loop
# was removed because the tier waits below use it too.
POLL_INTERVAL_S="${SWEEP_POLL_INTERVAL_S:-15}"
[ "$POLL_INTERVAL_S" -ge 1 ] 2>/dev/null || POLL_INTERVAL_S=1

FAILURES=()

log()  { printf '%s\n' "$*" >&2; }
fail() { FAILURES+=("$1"); log "FAILED: $1"; }

desired_for() { printf '%s' "${DESIRED[$1]:-1}"; }

# --- RDS --------------------------------------------------------------
log "--- starting ${DB_INSTANCE} ---"
start_rc=0
aws rds start-db-instance --db-instance-identifier "$DB_INSTANCE" \
  --query 'DBInstance.DBInstanceIdentifier' --output text >/dev/null 2>&1 || start_rc=$?

db_state=$(aws rds describe-db-instances --db-instance-identifier "$DB_INSTANCE" \
             --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null || echo "")
case "$db_state" in
  available|starting|configuring-enhanced-monitoring|modifying)
    if [ "$start_rc" -ne 0 ]; then
      # The daily "already running" case. Reported, not masked, and not
      # counted as a failure: the database is up, which is the whole
      # point of the step.
      log "${DB_INSTANCE}: start command exited ${start_rc} but instance is ${db_state} -- already running, continuing"
    else
      log "${DB_INSTANCE}: ${db_state}"
    fi
    ;;
  "")
    fail "could not read ${DB_INSTANCE} state after start (start exited ${start_rc})"
    ;;
  *)
    fail "${DB_INSTANCE} is ${db_state} after start (start exited ${start_rc}) -- app tier will come up against a stopped database"
    ;;
esac

# RDS reports `available` before it accepts connections on a cold start.
# The Azure version had the same gap and papered over it with the tier
# waits below; this is explicit so a slow database is a logged wait rather
# than a tier-1 crash loop.
log "--- waiting for ${DB_INSTANCE} to accept connections ---"
if ! aws rds wait db-instance-available --db-instance-identifier "$DB_INSTANCE" 2>/dev/null; then
  log "${DB_INSTANCE}: wait timed out or errored -- continuing, the tier waits will surface it"
fi

# --- ECS tiers --------------------------------------------------------
start_tier() {
  local label="$1"; shift
  log "--- ${label} ---"
  for svc in "$@"; do
    local want
    want=$(desired_for "$svc")
    # --query/--output: see shutdown-sweep.sh. These calls are what
    # produced 13,197 console lines across the two Azure jobs on
    # 2026-08-20..21.
    if aws ecs update-service \
         --cluster "$CLUSTER" --service "$svc" --desired-count "$want" \
         --query 'service.serviceName' --output text >/dev/null; then
      log "$svc: desired-count ${want}"
    else
      fail "desired-count ${want} on $svc"
    fi
  done
}

wait_stable() {
  local svc="$1"
  if aws ecs wait services-stable --cluster "$CLUSTER" --services "$svc" 2>/dev/null; then
    log "$svc: stable"
    return 0
  fi
  fail "$svc did not reach a stable state"
  return 1
}

start_tier "tier 1: foundational services (redis, qdrant, hatchet engine, sparse)" "${TIER1[@]}"
for svc in "${TIER1[@]}"; do
  wait_stable "$svc" || true
done

start_tier "tier 2: services that depend on tier 1" "${TIER2[@]}"
for svc in "${TIER2[@]}"; do
  wait_stable "$svc" || true
done

start_tier "tier 3: Laravel tier (depends on fastapi for the chat/query bridge)" "${TIER3[@]}"
for svc in "${TIER3[@]}"; do
  wait_stable "$svc" || true
done

# Services, plus the database. The endpoint term is gone with ADR-0023.
TOTAL=$(( SERVICE_COUNT + 1 ))
if [ ${#FAILURES[@]} -eq 0 ]; then
  log "startup sweep complete: ${TOTAL}/${TOTAL} actions succeeded and all readiness checks passed"
  exit 0
fi

log "startup sweep INCOMPLETE: ${#FAILURES[@]} problem(s)"
for f in "${FAILURES[@]}"; do
  log "  - $f"
done
exit 1
