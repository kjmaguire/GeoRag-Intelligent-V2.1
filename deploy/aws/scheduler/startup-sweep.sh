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
# New on AWS, and the sharpest edge the Bedrock route added (ADR-0022).
# Command A+ and Cohere Parse 5 run on SageMaker-managed endpoints that
# bill while they exist, so the shutdown sweep deletes them and this one
# recreates them from their retained endpoint configs.
#
# Recreation is not instant — a cold endpoint takes minutes to reach
# InService — and it can fail outright. A failed recreate is not a
# degraded path: it is no chat and no OCR at all, with NO Bedrock
# invocation metric to alarm on, because there are no invocations to
# fail. So this sweep waits for InService and does NOT report success
# without it. The `BEDROCK_ENDPOINT_NOT_INSERVICE` marker below is what
# the CloudWatch metric filter in deploy/aws/alerts/ matches.
#
# Endpoints are created FIRST, before the ECS tiers, because they are the
# slowest thing here by an order of magnitude and nothing in the app tier
# needs them to boot — only to answer.
set -uo pipefail

CLUSTER="${SWEEP_CLUSTER:-georag}"
DB_INSTANCE="${SWEEP_DB_INSTANCE:-georag-pg}"

TIER1=(redis qdrant hatchet sparse)
TIER2=(hatchet-worker fastapi martin)
TIER3=(laravel-octane laravel-horizon laravel-reverb)
SERVICE_COUNT=$(( ${#TIER1[@]} + ${#TIER2[@]} + ${#TIER3[@]} ))

# Octane runs two tasks; everything else runs one. The floor matters and
# the ceiling does not: at desired 1 every deploy and task replacement is
# a user-visible outage on the only public service. See ADR-0022 §3 and
# the cost reasoning preserved in deploy/azure/README.md.
declare -A DESIRED=( [laravel-octane]=2 )

# "name=endpoint-config-name" pairs. Empty means the deployment is on the
# hybrid Cohere-direct fallback and has no endpoints to manage.
read -r -a MARKETPLACE_ENDPOINTS <<< "${SWEEP_BEDROCK_ENDPOINTS:-}"
ENDPOINT_TIMEOUT_S="${SWEEP_ENDPOINT_TIMEOUT_S:-900}"
# Floored at 1. A zero interval makes the wait loop below spin without ever
# advancing its own clock, which is an infinite loop holding an ECS task
# open until the platform's own timeout kills it — found by the test
# harness passing 0 to make the cases fast.
POLL_INTERVAL_S="${SWEEP_POLL_INTERVAL_S:-15}"
[ "$POLL_INTERVAL_S" -ge 1 ] 2>/dev/null || POLL_INTERVAL_S=1

FAILURES=()

log()  { printf '%s\n' "$*" >&2; }
fail() { FAILURES+=("$1"); log "FAILED: $1"; }

desired_for() { printf '%s' "${DESIRED[$1]:-1}"; }

# --- Bedrock Marketplace endpoints ------------------------------------
recreate_endpoint() {
  local spec="$1"
  local name="${spec%%=*}"
  local config="${spec#*=}"
  [ "$config" = "$name" ] && config="${name}-config"

  local status
  status=$(aws sagemaker describe-endpoint --endpoint-name "$name" \
             --query EndpointStatus --output text 2>/dev/null || echo "")

  if [ "$status" = "InService" ]; then
    log "${name}: already InService"
    return 0
  fi

  if [ -z "$status" ]; then
    if ! aws sagemaker create-endpoint \
           --endpoint-name "$name" --endpoint-config-name "$config" \
           --query EndpointArn --output text >/dev/null; then
      fail "BEDROCK_ENDPOINT_NOT_INSERVICE ${name}: create-endpoint failed (config ${config})"
      return 1
    fi
    log "${name}: creating from config ${config}"
  else
    log "${name}: found in state ${status}, waiting"
  fi

  local waited=0
  while [ "$waited" -lt "$ENDPOINT_TIMEOUT_S" ]; do
    sleep "$POLL_INTERVAL_S"
    waited=$(( waited + POLL_INTERVAL_S ))
    status=$(aws sagemaker describe-endpoint --endpoint-name "$name" \
               --query EndpointStatus --output text 2>/dev/null || echo "")
    case "$status" in
      InService)
        log "${name}: InService after ${waited}s"
        return 0
        ;;
      Failed|OutOfService|RollingBack)
        fail "BEDROCK_ENDPOINT_NOT_INSERVICE ${name}: status ${status} after ${waited}s"
        return 1
        ;;
      *)
        log "${name}: ${status:-unknown} (${waited}s)"
        ;;
    esac
  done

  fail "BEDROCK_ENDPOINT_NOT_INSERVICE ${name}: still ${status:-unknown} after ${ENDPOINT_TIMEOUT_S}s"
  return 1
}

ENDPOINT_COUNT=0
if [ "${#MARKETPLACE_ENDPOINTS[@]}" -gt 0 ] && [ -n "${MARKETPLACE_ENDPOINTS[0]}" ]; then
  ENDPOINT_COUNT=${#MARKETPLACE_ENDPOINTS[@]}
  log "--- recreating ${ENDPOINT_COUNT} Bedrock Marketplace endpoint(s) ---"
  for spec in "${MARKETPLACE_ENDPOINTS[@]}"; do
    recreate_endpoint "$spec" || true
  done
else
  log "--- no Bedrock Marketplace endpoints configured, skipping ---"
fi

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

TOTAL=$(( SERVICE_COUNT + ENDPOINT_COUNT + 1 ))
if [ ${#FAILURES[@]} -eq 0 ]; then
  log "startup sweep complete: ${TOTAL}/${TOTAL} actions succeeded and all readiness checks passed"
  exit 0
fi

log "startup sweep INCOMPLETE: ${#FAILURES[@]} problem(s)"
for f in "${FAILURES[@]}"; do
  log "  - $f"
done
exit 1
