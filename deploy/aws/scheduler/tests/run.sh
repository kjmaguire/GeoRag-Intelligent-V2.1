#!/usr/bin/env bash
# Behavioural tests for the two nightly sweep scripts, run against
# tests/fake-aws rather than AWS. No credentials, no network, no mutation —
# the point is to pin the decisions the live jobs get wrong.
#
# Ported from deploy/azure/containerapps/scripts/tests/run.sh on 2026-09-08
# (ADR-0022). There is still no staging environment to rehearse a sweep on,
# so this harness remains the only thing standing between a scheduler edit
# and finding out at 08:30.
#
# Every case corresponds to something observed in production on Azure, or
# to a failure mode the Bedrock route newly introduced:
#
#   startup_db_already_running  the ServerIsNotStopped mask, which was 15
#                               of 15 retained days and every execution
#                               green while saying nothing true
#   startup_db_really_down      the same code path when it is not benign
#   shutdown_db_stop_no_op      an exit code that lies in the other
#                               direction, as on 2026-08-19 and 08-20
#   *_one_service_fails         one failed action must not strand the rest
#
# What is NOT here any more, deliberately:
#
#   dst_*         EventBridge Scheduler is timezone-aware, so there is one
#                 schedule, one fire and no guard to get wrong. See
#                 shutdown-sweep.sh's header.
#   endpoint_*    Removed 2026-09-15 with the code they covered (ADR-0023).
#                 They tested that a Marketplace endpoint failing to come
#                 back was a LOUD failure, because it left no chat and no
#                 OCR with no invocation metric to alarm on. Chat and OCR
#                 moved to Cohere's own API: no endpoint to recreate, so
#                 no failure mode to assert on. The fake-aws harness keeps
#                 its endpoint knobs (see tests/fake-aws) so the cases can
#                 come back with the code if a Marketplace endpoint is ever
#                 deployed deliberately.
#
# Usage: bash deploy/aws/scheduler/tests/run.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$(dirname "$HERE")"
SHUTDOWN="${SCRIPTS}/shutdown-sweep.sh"
STARTUP="${SCRIPTS}/startup-sweep.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "${WORK}/bin"
cp "${HERE}/fake-aws" "${WORK}/bin/aws"
chmod +x "${WORK}/bin/aws"

PASS=0
FAIL=0
CURRENT=""
OUT=""
RC=0

fail_case() { printf '  FAIL  %s: %s\n' "$CURRENT" "$1"; FAIL=$((FAIL + 1)); }

assert_rc() {
  if [ "$RC" -eq "$1" ]; then return 0; fi
  fail_case "expected exit ${1}, got ${RC}"
  printf '        --- output ---\n'
  sed 's/^/        /' <<< "$OUT"
}

assert_says() {
  grep -qF -- "$1" <<< "$OUT" || fail_case "output does not mention: $1"
}

assert_silent_about() {
  grep -qF -- "$1" <<< "$OUT" && fail_case "output should not mention: $1"
  return 0
}

assert_aws_calls() {
  # $1 = grep pattern, $2 = expected number of matching aws invocations.
  local n
  n=$(grep -cE -- "$1" "${WORK}/aws.log" 2>/dev/null || true)
  [ "${n:-0}" -eq "$2" ] || fail_case "expected ${2} aws calls matching /${1}/, saw ${n:-0}"
}

# run <case-name> <script> [VAR=VALUE ...]
run() {
  CURRENT="$1"; shift
  local script="$1"; shift
  : > "${WORK}/aws.log"
  rm -f "${WORK}/aws.log.created" "${WORK}/aws.log.deleted" "${WORK}/aws.log.dbstate"
  OUT=$(env -u FAKE_AWS_FAIL_SERVICES -u FAKE_AWS_DB_STOP_RC \
            -u FAKE_AWS_DB_START_RC -u FAKE_AWS_DB_STATE \
            -u FAKE_AWS_UNSTABLE -u FAKE_AWS_EP_STATES \
            -u FAKE_AWS_EP_CREATE_RC -u FAKE_AWS_EP_DELETE_RC \
            -u FAKE_AWS_EP_DELETE_TAKES_EFFECT -u FAKE_AWS_EP_AFTER_CREATE \
            PATH="${WORK}/bin:${PATH}" \
            FAKE_AWS_LOG="${WORK}/aws.log" \
            SWEEP_POLL_INTERVAL_S=1 \
            "$@" bash "$script" 2>&1)
  RC=$?
  PASS=$((PASS + 1))
}

# ---------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------

run shutdown_happy_path "$SHUTDOWN"
assert_rc 0
assert_says "shutdown sweep complete"
assert_aws_calls "ecs update-service" 10

run shutdown_scales_the_worker_too "$SHUTDOWN"
assert_rc 0
# The single biggest thing the Azure sweep could NOT do: hatchet-worker-cc
# was 4 vCPU / 8 GiB and never scaled in, because minReplicas is a floor
# and 29 crons meant it was never idle. ECS desired-count 0 stops it.
grep -qE "ecs update-service.*--service hatchet-worker" "${WORK}/aws.log" \
  || fail_case "the worker must be stopped, not just scaled"

run shutdown_one_service_fails "$SHUTDOWN" FAKE_AWS_FAIL_SERVICES="qdrant"
assert_rc 1
assert_says "FAILED: desired-count 0 on qdrant"
assert_says "shutdown sweep INCOMPLETE"
# The whole reason this script is not `set -e`: one failure must not
# strand the other nine services.
assert_aws_calls "ecs update-service" 10

run shutdown_db_stop_no_op "$SHUTDOWN" \
    FAKE_AWS_DB_STOP_RC=254 FAKE_AWS_DB_STATE=stopped
assert_rc 0
assert_says "treating as success"
assert_says "shutdown sweep complete"

run shutdown_db_stop_lies_the_other_way "$SHUTDOWN" \
    FAKE_AWS_DB_STOP_RC=0 FAKE_AWS_DB_STATE=available
# Exit code 0 with the instance still available IS a failure, and only a
# state read catches it. This is the case an exit-code check gets wrong.
assert_rc 1
assert_says "still available after stop"

run shutdown_db_unreadable "$SHUTDOWN" FAKE_AWS_DB_STATE=GONE
assert_rc 1
assert_says "could not read"

# A ratchet, not a scenario. The endpoint cases are gone (see the header),
# and the thing that must not come back is the CALL — the scheduler role no
# longer holds sagemaker:DeleteEndpoint, so a resurrected branch would fail
# with AccessDenied in the middle of the night rather than at review.
run shutdown_touches_no_endpoints "$SHUTDOWN" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat georag-parse"
assert_rc 0
assert_aws_calls "sagemaker delete-endpoint" 0
assert_aws_calls "sagemaker describe-endpoint" 0

# ---------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------

run startup_happy_path "$STARTUP"
assert_rc 0
assert_says "startup sweep complete"
assert_aws_calls "ecs update-service" 10

run startup_alb_services_get_two_tasks "$STARTUP"
assert_rc 0
# ADR-0022 §3: at desired 1 every deploy and task replacement is a
# user-visible outage on a public service.
grep -qE "ecs update-service.*--service laravel-octane --desired-count 2" "${WORK}/aws.log" \
  || fail_case "laravel-octane must come back at desired-count 2"
# Reverb is the other two-task service. It is asserted separately rather
# than as a loop over the table because the failure it guards against is
# the table and Terraform's local.services drifting apart, and a loop over
# the table could not see that.
grep -qE "ecs update-service.*--service laravel-reverb --desired-count 2" "${WORK}/aws.log" \
  || fail_case "laravel-reverb must come back at desired-count 2"
grep -qE "ecs update-service.*--service fastapi --desired-count 1" "${WORK}/aws.log" \
  || fail_case "everything else comes back at 1"

run startup_db_already_running "$STARTUP" \
    FAKE_AWS_DB_START_RC=254 FAKE_AWS_DB_STATE=available
# The daily case. Reported, not masked, and not counted as a failure: the
# database is up, which is the whole point of the step.
assert_rc 0
assert_says "already running, continuing"
assert_says "startup sweep complete"

run startup_db_really_down "$STARTUP" \
    FAKE_AWS_DB_START_RC=254 FAKE_AWS_DB_STATE=stopped
assert_rc 1
assert_says "app tier will come up against a stopped database"

run startup_one_service_fails "$STARTUP" FAKE_AWS_FAIL_SERVICES="redis"
assert_rc 1
assert_says "FAILED: desired-count 1 on redis"
# Tier 1 failing must not stop tiers 2 and 3 from being attempted: leaving
# the platform down for the working day is worse than a partial start.
assert_aws_calls "ecs update-service" 10

run startup_unstable_service_is_a_failure "$STARTUP" FAKE_AWS_UNSTABLE="fastapi"
assert_rc 1
assert_says "fastapi did not reach a stable state"

# The matching ratchet. The startup sweep used to block for up to 15
# minutes waiting for an endpoint to reach InService; it must not do that
# again by accident, and it must not call an API the role cannot use.
run startup_touches_no_endpoints "$STARTUP" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat=georag-chat-config"
assert_rc 0
assert_aws_calls "sagemaker create-endpoint" 0
assert_aws_calls "sagemaker describe-endpoint" 0
assert_silent_about "BEDROCK_ENDPOINT_NOT_INSERVICE"

# ---------------------------------------------------------------------

printf '\n%d case(s) run, %d failure(s)\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
