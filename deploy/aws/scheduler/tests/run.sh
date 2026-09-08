#!/usr/bin/env bash
# Behavioural tests for the two nightly sweep scripts, run against
# tests/fake-aws rather than AWS. No credentials, no network, no mutation —
# the point is to pin the decisions the live jobs get wrong.
#
# Ported from deploy/azure/containerapps/scripts/tests/run.sh on 2026-09-08
# (ADR-0022). There is still no staging environment to rehearse a sweep on,
# so this harness remains the only thing standing between a scheduler edit
# and finding out at 06:00.
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
#   endpoint_*                  NEW on AWS: a Marketplace endpoint that
#                               fails to come back leaves no chat and no
#                               OCR at all, with no Bedrock invocation
#                               metric to alarm on — there are no
#                               invocations to fail. The sweep must not
#                               report success without InService.
#
# What is NOT here any more, deliberately: the dst_* cases. EventBridge
# Scheduler is timezone-aware, so there is one schedule, one fire and no
# guard to get wrong. See shutdown-sweep.sh's header.
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
            SWEEP_ENDPOINT_TIMEOUT_S=2 \
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

run shutdown_no_endpoints_configured "$SHUTDOWN"
assert_rc 0
assert_says "no Bedrock Marketplace endpoints configured"
assert_aws_calls "sagemaker delete-endpoint" 0

run shutdown_deletes_endpoints "$SHUTDOWN" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat georag-parse" \
    FAKE_AWS_EP_STATES="georag-chat:InService georag-parse:InService"
assert_rc 0
assert_says "georag-chat: deleted"
assert_says "georag-parse: deleted"
assert_aws_calls "sagemaker delete-endpoint" 2

run shutdown_endpoint_already_gone "$SHUTDOWN" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat" \
    FAKE_AWS_EP_STATES="georag-chat:MISSING" FAKE_AWS_EP_DELETE_RC=254 \
    FAKE_AWS_EP_DELETE_TAKES_EFFECT=1
# A previous run already deleted it. The command fails; the end state is
# what was wanted. Same reasoning as the Postgres case.
assert_rc 0
assert_says "already deleted"

run shutdown_endpoint_delete_really_failed "$SHUTDOWN" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat" \
    FAKE_AWS_EP_STATES="georag-chat:InService" FAKE_AWS_EP_DELETE_RC=254
# Delete failed AND the endpoint is still there — this one really is a
# failure, and it costs money every hour it goes unnoticed.
assert_rc 1
assert_says "still exists after delete"

# ---------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------

run startup_happy_path "$STARTUP"
assert_rc 0
assert_says "startup sweep complete"
assert_aws_calls "ecs update-service" 10

run startup_octane_gets_two_tasks "$STARTUP"
assert_rc 0
# ADR-0022 §3: at desired 1 every deploy and task replacement is a
# user-visible outage on the only public service.
grep -qE "ecs update-service.*--service laravel-octane --desired-count 2" "${WORK}/aws.log" \
  || fail_case "laravel-octane must come back at desired-count 2"
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

run startup_recreates_endpoints "$STARTUP" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat=georag-chat-config georag-parse=georag-parse-config"
assert_rc 0
assert_says "georag-chat: creating from config georag-chat-config"
assert_says "startup sweep complete"
assert_aws_calls "sagemaker create-endpoint" 2

run startup_endpoint_already_inservice "$STARTUP" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat" \
    FAKE_AWS_EP_STATES="georag-chat:InService"
assert_rc 0
assert_says "already InService"
assert_aws_calls "sagemaker create-endpoint" 0

run startup_endpoint_create_fails "$STARTUP" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat" FAKE_AWS_EP_CREATE_RC=254
assert_rc 1
assert_says "BEDROCK_ENDPOINT_NOT_INSERVICE"
assert_says "startup sweep INCOMPLETE"

run startup_endpoint_lands_failed "$STARTUP" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat" FAKE_AWS_EP_AFTER_CREATE=Failed
assert_rc 1
assert_says "BEDROCK_ENDPOINT_NOT_INSERVICE"
assert_says "status Failed"

run startup_endpoint_never_ready "$STARTUP" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat" FAKE_AWS_EP_AFTER_CREATE=Creating
# The timeout case: the sweep must NOT report complete just because the
# create call succeeded. An endpoint stuck in Creating is a platform with
# no chat.
assert_rc 1
assert_says "BEDROCK_ENDPOINT_NOT_INSERVICE"
assert_silent_about "startup sweep complete"

run startup_endpoint_failure_does_not_stop_the_tiers "$STARTUP" \
    SWEEP_BEDROCK_ENDPOINTS="georag-chat" FAKE_AWS_EP_CREATE_RC=254
assert_rc 1
# Same independence rule as everywhere else: no chat is bad, no platform
# is worse. Every service is still started.
assert_aws_calls "ecs update-service" 10

# ---------------------------------------------------------------------

printf '\n%d case(s) run, %d failure(s)\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
