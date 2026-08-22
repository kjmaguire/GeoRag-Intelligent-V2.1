#!/usr/bin/env bash
# Behavioural tests for the two nightly sweep scripts, run against
# tests/fake-az rather than Azure. No credentials, no network, no
# mutation — the point is to pin the decisions the live jobs get wrong.
#
# Every case here corresponds to something observed in production:
#
#   startup_pg_already_running   the ServerIsNotStopped mask, 15 of 15
#                                retained days, every execution green
#   startup_pg_really_down       the same code path when it is not benign
#   shutdown_pg_stop_no_op       an exit code that lies in the other
#                                direction, as on 2026-08-19 and 08-20
#   *_one_app_fails              one failed action must not strand the
#                                other seven
#   dst_*                        the transition-day hour, which the
#                                previous 00:00-UTC arithmetic got wrong
#
# Usage: bash deploy/azure/containerapps/scripts/tests/run.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$(dirname "$HERE")"
SHUTDOWN="${SCRIPTS}/shutdown-sweep.sh"
STARTUP="${SCRIPTS}/startup-sweep.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "${WORK}/bin"
cp "${HERE}/fake-az" "${WORK}/bin/az"
chmod +x "${WORK}/bin/az"

PASS=0
FAIL=0
CURRENT=""
OUT=""
RC=0

# Epochs the DST cases stand on. 2026-03-08 is the second Sunday of March
# and 2026-11-01 the first Sunday of November; for US-Pacific the
# transitions land at 10:00 and 09:00 UTC (02:00 PST and 02:00 PDT).
PDT_BEGINS=$(date -u -d "2026-03-08T10:00:00Z" +%s)
PST_BEGINS=$(date -u -d "2026-11-01T09:00:00Z" +%s)

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

assert_az_calls() {
  # $1 = grep pattern, $2 = expected number of matching az invocations.
  local n
  n=$(grep -cE -- "$1" "${WORK}/az.log" 2>/dev/null || true)
  [ "${n:-0}" -eq "$2" ] || fail_case "expected ${2} az calls matching /${1}/, saw ${n:-0}"
}

# run <case-name> <script> [VAR=VALUE ...]
run() {
  CURRENT="$1"; shift
  local script="$1"; shift
  : > "${WORK}/az.log"
  OUT=$(env -u FAKE_AZ_LOGIN_RC -u FAKE_AZ_FAIL_APPS -u FAKE_AZ_PG_STOP_RC \
            -u FAKE_AZ_PG_START_RC -u FAKE_AZ_PG_STATE \
            -u FAKE_AZ_NOT_RUNNING -u FAKE_AZ_NOT_HEALTHY \
            PATH="${WORK}/bin:${PATH}" FAKE_AZ_LOG="${WORK}/az.log" \
            SWEEP_WAIT_TIMEOUT=1 SWEEP_WAIT_INTERVAL=1 \
            "$@" bash "$script" 2>&1)
  RC=$?
  printf '  ....  %s\n' "$CURRENT"
}

done_case() { [ "$FAIL" -eq "$BEFORE" ] && PASS=$((PASS + 1)); }

check() {
  BEFORE=$FAIL
  "$@"
  done_case
}

# A fixed instant that is unambiguously inside PDT, so the guard passes
# for every case that is not about the guard. 2026-08-21T06:00Z is 23:00
# US-Pacific (the shutdown target, on the previous local day) and
# 2026-08-21T13:00Z is 06:00 (the startup target).
SHUTDOWN_HOUR=$(date -u -d "2026-08-21T06:00:00Z" +%s)
STARTUP_HOUR=$(date -u -d "2026-08-21T13:00:00Z" +%s)

echo "shutdown-sweep.sh"

check_shutdown_happy() {
  run shutdown_happy "$SHUTDOWN" "SWEEP_NOW_EPOCH=$SHUTDOWN_HOUR" FAKE_AZ_PG_STATE=Stopped
  assert_rc 0
  assert_says "shutdown sweep complete: 9/9 actions succeeded"
  assert_az_calls "^containerapp update" 8
  assert_az_calls "^postgres flexible-server stop" 1
}
check check_shutdown_happy

check_shutdown_one_app_fails() {
  run shutdown_one_app_fails "$SHUTDOWN" "SWEEP_NOW_EPOCH=$SHUTDOWN_HOUR" \
      FAKE_AZ_PG_STATE=Stopped FAKE_AZ_FAIL_APPS=qdrant-cc
  assert_rc 1
  assert_says "min-replicas 0 on qdrant-cc"
  assert_says "shutdown sweep INCOMPLETE: 1 of 9 actions failed"
  # The whole point: the sweep carried on past the failure.
  assert_az_calls "^containerapp update" 8
  assert_az_calls "^postgres flexible-server stop" 1
}
check check_shutdown_one_app_fails

check_shutdown_pg_stop_no_op() {
  # The command exits non-zero but the server did reach Stopped — the
  # 2026-08-19/08-20 shape, where the CLI's poll was interrupted after
  # ARM had already accepted the stop. Not a failure.
  run shutdown_pg_stop_no_op "$SHUTDOWN" "SWEEP_NOW_EPOCH=$SHUTDOWN_HOUR" \
      FAKE_AZ_PG_STOP_RC=1 FAKE_AZ_PG_STATE=Stopped
  assert_rc 0
  assert_says "stop command exited 1 but server is Stopped -- treating as success"
  assert_says "shutdown sweep complete"
}
check check_shutdown_pg_stop_no_op

check_shutdown_pg_still_ready() {
  # The inverse, and the one an exit-code check cannot catch: the command
  # succeeded and the server is still serving.
  run shutdown_pg_still_ready "$SHUTDOWN" "SWEEP_NOW_EPOCH=$SHUTDOWN_HOUR" \
      FAKE_AZ_PG_STOP_RC=0 FAKE_AZ_PG_STATE=Ready
  assert_rc 1
  assert_says "georag-pg-cc is still Ready after stop"
  assert_silent_about "shutdown sweep complete"
}
check check_shutdown_pg_still_ready

check_shutdown_pg_state_unreadable() {
  run shutdown_pg_state_unreadable "$SHUTDOWN" "SWEEP_NOW_EPOCH=$SHUTDOWN_HOUR" \
      FAKE_AZ_PG_STOP_RC=0 FAKE_AZ_PG_STATE=
  assert_rc 1
  assert_says "could not read georag-pg-cc state after stop"
}
check check_shutdown_pg_state_unreadable

check_shutdown_login_fails() {
  run shutdown_login_fails "$SHUTDOWN" "SWEEP_NOW_EPOCH=$SHUTDOWN_HOUR" FAKE_AZ_LOGIN_RC=1
  assert_rc 1
  assert_says "FATAL: az login --identity failed; no action taken"
  assert_az_calls "^containerapp update" 0
}
check check_shutdown_login_fails

echo "startup-sweep.sh"

check_startup_happy() {
  run startup_happy "$STARTUP" "SWEEP_NOW_EPOCH=$STARTUP_HOUR" FAKE_AZ_PG_STATE=Ready
  assert_rc 0
  assert_says "startup sweep complete: 9/9"
  assert_az_calls "^containerapp update" 8
}
check check_startup_happy

check_startup_pg_already_running() {
  # The live case: `start` fails with ServerIsNotStopped because an
  # operator restarted the stack inside the shutdown window. The database
  # is up, so the sweep must go green — while still saying what happened.
  run startup_pg_already_running "$STARTUP" "SWEEP_NOW_EPOCH=$STARTUP_HOUR" \
      FAKE_AZ_PG_START_RC=1 FAKE_AZ_PG_STATE=Ready
  assert_rc 0
  assert_says "start command exited 1 but server is Ready -- already running, continuing"
  assert_says "startup sweep complete"
  # And it must not be reported the way the old script did.
  assert_silent_about "skip postgres"
}
check check_startup_pg_already_running

check_startup_pg_really_down() {
  # Same command, same exit code, different state. This is the failure the
  # `|| echo "skip postgres"` mask made indistinguishable from the one
  # above: eight apps brought up against a stopped database, execution
  # green, nobody told.
  run startup_pg_really_down "$STARTUP" "SWEEP_NOW_EPOCH=$STARTUP_HOUR" \
      FAKE_AZ_PG_START_RC=1 FAKE_AZ_PG_STATE=Stopped
  assert_rc 1
  assert_says "app tier will come up against a stopped database"
  assert_silent_about "startup sweep complete"
  # Still brings the apps up — a stopped database is a reason to shout,
  # not a reason to leave the platform down for the working day.
  assert_az_calls "^containerapp update" 8
}
check check_startup_pg_really_down

check_startup_wait_times_out() {
  run startup_wait_times_out "$STARTUP" "SWEEP_NOW_EPOCH=$STARTUP_HOUR" \
      FAKE_AZ_PG_STATE=Ready FAKE_AZ_NOT_HEALTHY=fastapi-cc
  assert_rc 1
  assert_says "fastapi-cc did not reach Healthy within 1s (healthState=Unhealthy)"
  # Tier 3 is still started: the tiering is an ordering preference, not a
  # gate, and that has to stay true or a slow fastapi-cc takes the site
  # down for the day.
  assert_az_calls "^containerapp update" 8
}
check check_startup_wait_times_out

check_startup_counts_every_problem() {
  # An app whose update failed will also fail its readiness wait, so
  # this is what one bad app plus one slow app really looks like:
  # three entries, each naming the app and the step.
  run startup_counts_every_problem "$STARTUP" "SWEEP_NOW_EPOCH=$STARTUP_HOUR" \
      FAKE_AZ_PG_STATE=Ready FAKE_AZ_FAIL_APPS=redis-cc \
      "FAKE_AZ_NOT_RUNNING=redis-cc laravel-reverb-cc"
  assert_rc 1
  assert_says "startup sweep INCOMPLETE: 3 problem(s)"
  assert_says "min-replicas 1 on redis-cc"
  assert_says "redis-cc did not reach RunningAtMaxScale"
  assert_says "laravel-reverb-cc did not reach RunningAtMaxScale"
}
check check_startup_counts_every_problem

echo "DST guard"

check_dst_march_pst_side() {
  # 2026-03-08T07:00Z is 23:00 PST on 03-07 — the correct shutdown hour,
  # because the switch to PDT does not happen until 10:00Z that day. The
  # old arithmetic put the transition at 00:00Z, read the offset as PDT,
  # computed local hour 00 and skipped — having already fired an hour
  # early at 06:00Z.
  run dst_march_pst_side "$SHUTDOWN" "SWEEP_NOW_EPOCH=$((PDT_BEGINS - 3 * 3600))" \
      FAKE_AZ_PG_STATE=Stopped
  assert_rc 0
  assert_says "shutdown sweep complete"
  assert_silent_about "DST-safety double-fire"
}
check check_dst_march_pst_side

check_dst_march_skips_early_hour() {
  # The other candidate hour on the same day: 06:00Z is 22:00 PST.
  run dst_march_skips_early_hour "$SHUTDOWN" "SWEEP_NOW_EPOCH=$((PDT_BEGINS - 4 * 3600))"
  assert_rc 0
  assert_says "US-Pacific local hour is 22 (offset -8h), not 23 -- skipping"
  assert_az_calls "." 0
}
check check_dst_march_skips_early_hour

check_dst_november_pdt_side() {
  # 2026-11-01T06:00Z is 23:00 PDT on 10-31; PST does not begin until
  # 09:00Z. The old arithmetic ended DST at 00:00Z that day, so it read
  # PST, computed 22 and skipped — then fired at 07:00Z, which is 00:00
  # PDT, an hour late.
  run dst_november_pdt_side "$SHUTDOWN" "SWEEP_NOW_EPOCH=$((PST_BEGINS - 3 * 3600))" \
      FAKE_AZ_PG_STATE=Stopped
  assert_rc 0
  assert_says "shutdown sweep complete"
  assert_silent_about "DST-safety double-fire"
}
check check_dst_november_pdt_side

check_dst_november_skips_late_hour() {
  # The other candidate hour on the same day: 07:00Z is 00:00 PDT.
  run dst_november_skips_late_hour "$SHUTDOWN" "SWEEP_NOW_EPOCH=$((PST_BEGINS - 2 * 3600))"
  assert_rc 0
  assert_says "US-Pacific local hour is 00 (offset -7h), not 23 -- skipping"
  assert_az_calls "." 0
}
check check_dst_november_skips_late_hour

check_dst_startup_fires_at_06_local() {
  # The startup job's own target hour, checked in winter so it takes
  # the other branch of the guard: 2026-01-15T14:00Z is 06:00 PST.
  run dst_startup_fires_at_06_local "$STARTUP" \
      "SWEEP_NOW_EPOCH=$(date -u -d '2026-01-15T14:00:00Z' +%s)" FAKE_AZ_PG_STATE=Ready
  assert_rc 0
  assert_says "startup sweep complete"
  assert_silent_about "DST-safety double-fire"
}
check check_dst_startup_fires_at_06_local

check_dst_startup_skips_other_hour() {
  run dst_startup_skips_other_hour "$STARTUP" \
      "SWEEP_NOW_EPOCH=$(date -u -d '2026-01-15T13:00:00Z' +%s)"
  assert_rc 0
  assert_says "US-Pacific local hour is 05 (offset -8h), not 06 -- skipping"
  assert_az_calls "." 0
}
check check_dst_startup_skips_other_hour

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
