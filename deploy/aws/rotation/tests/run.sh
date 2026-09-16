#!/usr/bin/env bash
# Dry rehearsal of the APP_KEY rotation, against fakes instead of AWS and
# the Laravel image. No credentials, no network, no PHP, no mutation.
#
# This replaces deploy/azure/containerapps/tests/rotate-app-key.test.sh,
# deleted with the Azure tree on 2026-09-08. ops/runbooks/secret-rotation.md
# §2 recorded what that cost: "that harness went with the script, so the
# decisions below are no longer pinned by anything." They are again.
#
# There is still no staging environment — the only AWS account is
# production, exactly as the only Azure resource group was — so this is the
# ONLY rehearsal an APP_KEY rotation gets before it runs against real data.
# Every case below is a way a rotation destroys the audit ledger or lies
# about having rotated it:
#
#   inside_*   rotate-app-key-inside.sh against a fake `php artisan`. The
#              restore must run under APP_KEY_NEXT and the dump under the
#              current key — reversed, it re-encrypts every row to the key
#              it already had, exits 0, and is indistinguishable from
#              success. A dump failure must change nothing; a restore
#              failure must be reported as its own exit code, because it
#              is the one state the outer script must not scale traffic
#              back into.
#
#   outer_*    rotate-app-key.sh against a fake `aws`. Ordering is most of
#              it: no secret written before the snapshot exists, no task
#              run before the writers are at zero, no promotion before the
#              task exits 0, no traffic after a restore failure.
#
# Usage: bash deploy/aws/rotation/tests/run.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OUTER="${ROOT}/rotate-app-key.sh"
INSIDE="${ROOT}/rotate-app-key-inside.sh"

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq is not installed; the rotation requires it." >&2; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "${WORK}/bin"
cp "${HERE}/fake-aws" "${WORK}/bin/aws"
cp "${HERE}/fake-php" "${WORK}/bin/php"
chmod +x "${WORK}/bin/aws" "${WORK}/bin/php"

PASS=0
FAIL=0
CURRENT=""
OUT=""
RC=0
STATE=""

fail_case() { printf '  FAIL  %s: %s\n' "$CURRENT" "$1"; FAIL=$((FAIL + 1)); }

assert_rc() {
  [ "$RC" -eq "$1" ] && return 0
  fail_case "expected exit ${1}, got ${RC}"
  printf '        --- output ---\n'
  sed 's/^/        /' <<< "$OUT"
}

assert_says() { grep -qF -- "$1" <<< "$OUT" || fail_case "output does not mention: $1"; }
assert_silent_about() { grep -qF -- "$1" <<< "$OUT" && fail_case "output should not mention: $1"; return 0; }

assert_aws_called()     { grep -qE -- "$1" "${STATE}/aws.log" || fail_case "aws was never called matching: $1"; }
assert_aws_not_called() { grep -qE -- "$1" "${STATE}/aws.log" && fail_case "aws should not have been called matching: $1"; return 0; }

# Ordering assertions are the point of several cases, so they get a helper
# rather than being open-coded and subtly different each time.
assert_aws_order() {
  local first last a b
  first="$1"; last="$2"
  a="$(grep -nE -- "$first" "${STATE}/aws.log" | head -1 | cut -d: -f1)"
  b="$(grep -nE -- "$last"  "${STATE}/aws.log" | head -1 | cut -d: -f1)"
  if [ -z "$a" ] || [ -z "$b" ]; then
    fail_case "cannot order '${first}' before '${last}': one of them never happened"
    return
  fi
  [ "$a" -lt "$b" ] || fail_case "'${first}' (line ${a}) should come before '${last}' (line ${b})"
}

assert_php_called()     { grep -qE -- "$1" "${STATE}/php.log" || fail_case "php artisan was never called matching: $1"; }
assert_php_not_called() { grep -qE -- "$1" "${STATE}/php.log" && fail_case "php artisan should not have been called matching: $1"; return 0; }

assert_secret_has()    { jq -e --arg k "$1" 'has($k)'     >/dev/null < "${STATE}/secret.json" || fail_case "secret should still have key: $1"; }
assert_secret_lacks()  { jq -e --arg k "$1" 'has($k)|not' >/dev/null < "${STATE}/secret.json" || fail_case "secret should no longer have key: $1"; }
assert_secret_eq()     { local got; got="$(jq -r --arg k "$1" '.[$k] // ""' < "${STATE}/secret.json")"
                         [ "$got" = "$2" ] || fail_case "secret.$1 is '${got}', expected '${2}'"; }
assert_secret_ne()     { local got; got="$(jq -r --arg k "$1" '.[$k] // ""' < "${STATE}/secret.json")"
                         [ "$got" != "$2" ] || fail_case "secret.$1 should no longer be '${2}'"; }

assert_desired()       { local got; got="$(cat "${STATE}/desired.$1" 2>/dev/null || echo unset)"
                         [ "$got" = "$2" ] || fail_case "$1 desiredCount is '${got}', expected '${2}'"; }

# --apply mints its own key, so a case cannot name the value it expects —
# only that it is a well-formed Laravel key and is not the one it replaced.
assert_secret_is_a_fresh_key() {
  local got; got="$(jq -r '.APP_KEY // ""' < "${STATE}/secret.json")"
  grep -qE '^base64:[A-Za-z0-9+/]{42,}=*$' <<< "$got" || fail_case "APP_KEY is not a well-formed key: '${got}'"
  [ "$got" != "$OLD_KEY" ] || fail_case "APP_KEY was not changed"
}

# What the secret's APP_KEY was when run-task number $1 started.
assert_appkey_at_runtask() {
  local got; got="$(cat "${STATE}/appkey-at-runtask.$1" 2>/dev/null || echo missing)"
  [ "$got" = "$2" ] || fail_case "APP_KEY at run-task #${1} was '${got}', expected '${2}'"
}

OLD_KEY="base64:b2xkb2xkb2xkb2xkb2xkb2xkb2xkb2xkb2xkb2xkb28="

# fresh_state [extra jq filter applied to the secret]
fresh_state() {
  STATE="$(mktemp -d "${WORK}/state.XXXXXX")"
  : > "${STATE}/aws.log"
  : > "${STATE}/php.log"
  jq -n --arg k "$OLD_KEY" '{
    APP_KEY: $k, FASTAPI_SERVICE_KEY: "svc", QDRANT_API_KEY: "qd",
    HATCHET_CLIENT_TOKEN: "hc", REDIS_PASSWORD: "rp",
    FLOW_JWT_SECRET: "fj", REVERB_APP_SECRET: "rs"
  }' > "${STATE}/secret.json"
  # The counts the rotation must discover and put back. Octane at 3, not 2,
  # on purpose: a script that restores a hardcoded count passes at 2.
  printf '3' > "${STATE}/desired.laravel-octane";  printf '3' > "${STATE}/initial.laravel-octane"
  printf '1' > "${STATE}/desired.laravel-horizon"; printf '1' > "${STATE}/initial.laravel-horizon"
  printf '2' > "${STATE}/desired.laravel-reverb";  printf '2' > "${STATE}/initial.laravel-reverb"
}

# run_outer <case> [VAR=VALUE ...] [-- <script args>]
run_outer() {
  CURRENT="$1"; shift
  fresh_state
  local -a env_pairs=() args=()
  local seen_dashdash=0 a
  for a in "$@"; do
    if [ "$a" = "--" ]; then seen_dashdash=1; continue; fi
    if [ "$seen_dashdash" = 1 ]; then args+=("$a"); else env_pairs+=("$a"); fi
  done
  OUT="$(
    env PATH="${WORK}/bin:${PATH}" \
        FAKE_AWS_LOG="${STATE}/aws.log" \
        FAKE_AWS_STATE="${STATE}" \
        ROTATE_CLUSTER=georag \
        ROTATE_SECRET_ID=georag/app \
        ROTATE_SUBNETS=subnet-a \
        ROTATE_SECURITY_GROUP=sg-a \
        ROTATE_WAIT_TRIES=2 \
        ROTATE_WAIT_INTERVAL=0 \
        "${env_pairs[@]}" \
        bash "$OUTER" "${args[@]}" 2>&1
  )"
  RC=$?
}

# run_inside <case> [VAR=VALUE ...]
run_inside() {
  CURRENT="$1"; shift
  fresh_state
  OUT="$(
    env PATH="${WORK}/bin:${PATH}" \
        FAKE_PHP_LOG="${STATE}/php.log" \
        ROTATE_SECURE_DIR="${STATE}/secure" \
        ROTATE_ARTISAN="php artisan" \
        "$@" \
        bash "$INSIDE" 2>&1
  )"
  RC=$?
}

done_case() { [ "$FAIL" -eq "$PREV_FAIL" ] && PASS=$((PASS + 1)); PREV_FAIL="$FAIL"; }
PREV_FAIL=0

NEW_KEY="base64:bmV3bmV3bmV3bmV3bmV3bmV3bmV3bmV3bmV3bmV3bmU="

echo "== rotate-app-key-inside.sh =="

# The single most important assertion in this file. Backwards, this is a
# silent no-op rotation.
run_inside inside_restore_runs_under_the_new_key \
  APP_KEY="$OLD_KEY" APP_KEY_NEXT="$NEW_KEY"
assert_rc 0
assert_php_called "audit:dump-pii APP_KEY=${OLD_KEY}"
assert_php_called "audit:restore-pii APP_KEY=${NEW_KEY}"
assert_says ROTATE_OK
done_case

run_inside inside_dump_failure_changes_nothing \
  APP_KEY="$OLD_KEY" APP_KEY_NEXT="$NEW_KEY" FAKE_PHP_DUMP_RC=1
assert_rc 10
assert_php_not_called "audit:restore-pii"
assert_says "stage=dump"
done_case

run_inside inside_restore_failure_is_its_own_exit_code \
  APP_KEY="$OLD_KEY" APP_KEY_NEXT="$NEW_KEY" FAKE_PHP_RESTORE_RC=1
assert_rc 20
assert_says "stage=restore"
done_case

# audit:restore-pii is idempotent (RestoreAuditPii.php:117-146), so one
# retry is free and saves the heavy snapshot recovery on a transient blip.
run_inside inside_restore_is_retried_once_before_giving_up \
  APP_KEY="$OLD_KEY" APP_KEY_NEXT="$NEW_KEY" FAKE_PHP_RESTORE_RC=1 FAKE_PHP_RESTORE_RC_2=0
assert_rc 0
assert_says ROTATE_RETRY
done_case

run_inside inside_refuses_an_empty_next_key \
  APP_KEY="$OLD_KEY" APP_KEY_NEXT=""
assert_rc 64
assert_php_not_called "audit:"
done_case

run_inside inside_refuses_a_malformed_next_key \
  APP_KEY="$OLD_KEY" APP_KEY_NEXT="not-a-laravel-key"
assert_rc 64
assert_php_not_called "audit:"
done_case

# Equal keys would re-encrypt every row to the key it already had and exit
# 0 — a rotation that rotated nothing, reported as success.
run_inside inside_refuses_when_next_equals_current \
  APP_KEY="$OLD_KEY" APP_KEY_NEXT="$OLD_KEY"
assert_rc 64
assert_php_not_called "audit:"
done_case

run_inside inside_shreds_the_plaintext_dump \
  APP_KEY="$OLD_KEY" APP_KEY_NEXT="$NEW_KEY"
assert_rc 0
if ls "${STATE}/secure/"*.jsonl >/dev/null 2>&1; then
  fail_case "a plaintext dump survived a successful rotation"
fi
done_case

echo "== rotate-app-key.sh =="

run_outer outer_dry_run_mutates_nothing
assert_rc 0
assert_aws_not_called "create-db-snapshot"
assert_aws_not_called "put-secret-value"
assert_aws_not_called "update-service"
assert_aws_not_called "run-task"
assert_says "dry run"
done_case

run_outer outer_refuses_a_stopped_database FAKE_AWS_DB_STATE=stopped -- --apply
assert_rc 1
assert_aws_not_called "create-db-snapshot"
assert_aws_not_called "put-secret-value"
assert_says "maintenance window"
done_case

run_outer outer_refuses_a_missing_task_definition FAKE_AWS_NO_TASKDEF=1 -- --apply
assert_rc 1
assert_aws_not_called "create-db-snapshot"
assert_says "rotation.tf"
done_case

# The snapshot IS the recovery path for a half-re-encrypted ledger. Nothing
# may be staged, and nothing scaled down, until it exists.
run_outer outer_no_snapshot_means_no_rotation FAKE_AWS_SNAPSHOT_RC=1 -- --apply
assert_rc 1
assert_aws_not_called "put-secret-value"
assert_aws_not_called "update-service"
assert_says "Refusing to re-encrypt without one"
done_case

run_outer outer_a_snapshot_that_never_arrives_stops_it FAKE_AWS_SNAPSHOT_WAIT_RC=1 -- --apply
assert_rc 1
assert_aws_not_called "put-secret-value"
assert_says "Refusing to re-encrypt without one"
done_case

run_outer outer_snapshot_precedes_every_mutation -- --apply
assert_rc 0
assert_aws_order "create-db-snapshot" "put-secret-value"
assert_aws_order "create-db-snapshot" "update-service"
done_case

# Both writers of encrypted columns must be at zero before the task runs.
# laravel-horizon is here because StreamQueryFromFastApi writes
# response_text on three paths — the Azure runbook's finding 5 said it did
# not, and was wrong.
run_outer outer_quiesces_both_writers_before_running_the_task -- --apply
assert_rc 0
assert_aws_called "update-service .*laravel-octane .*--desired-count 0"
assert_aws_called "update-service .*laravel-horizon .*--desired-count 0"
assert_aws_order "laravel-octane .*--desired-count 0" "run-task"
assert_aws_order "laravel-horizon .*--desired-count 0" "run-task"
done_case

# Reverb holds no audit rows; stopping it would drop every open WebSocket
# for nothing. It is rolled, not quiesced.
run_outer outer_does_not_stop_reverb -- --apply
assert_rc 0
assert_aws_not_called "update-service .*laravel-reverb .*--desired-count 0"
assert_aws_called "update-service .*laravel-reverb .*--force-new-deployment"
done_case

# A writer that never actually comes down is the failure the runningCount
# poll exists for: `update-service` returning 0 says the API accepted the
# change, not that the tasks stopped.
run_outer outer_refuses_a_writer_that_will_not_come_down \
  FAKE_AWS_STUCK_RUNNING=laravel-octane -- --apply
assert_rc 1
assert_aws_not_called "run-task"
assert_secret_lacks APP_KEY_NEXT
assert_secret_eq APP_KEY "$OLD_KEY"
assert_desired laravel-octane 3
done_case

# The staging write legitimately precedes run-task, so ordering the two
# API calls proves nothing. What matters is the VALUE: the re-encryption
# task must start while APP_KEY is still the old key, and the verification
# task (#2) must start after it is the new one.
run_outer outer_promotes_nothing_before_the_task_succeeds -- --apply
assert_rc 0
assert_appkey_at_runtask 1 "$OLD_KEY"
assert_secret_is_a_fresh_key
done_case

# Exit 10 from the inside half means the dump failed and nothing changed.
# Fully reversible, and reversed here rather than left for a human.
run_outer outer_a_dump_failure_is_fully_reversed FAKE_AWS_TASK_EXIT=10 -- --apply
assert_rc 1
assert_secret_eq APP_KEY "$OLD_KEY"
assert_secret_lacks APP_KEY_NEXT
assert_secret_lacks APP_KEY_ROTATION_RESTORE
assert_desired laravel-octane 3
assert_desired laravel-horizon 1
assert_says ABANDONED
done_case

# Exit 20 is the one state that must NOT be cleaned up: a half-re-encrypted
# ledger must not serve traffic, and putting the old key back makes it no
# more readable.
run_outer outer_a_restore_failure_leaves_everything_down FAKE_AWS_TASK_EXIT=20 -- --apply
assert_rc 1
assert_desired laravel-octane 0
assert_desired laravel-horizon 0
assert_secret_has APP_KEY_NEXT
assert_secret_eq APP_KEY "$OLD_KEY"
assert_says "must not serve traffic"
assert_says "-preappkey-"
done_case

# A task killed outright reports no exit code at all. That is not success,
# and it is not distinguishable from a kill mid-restore.
run_outer outer_a_task_with_no_exit_code_is_treated_as_a_restore_failure \
  FAKE_AWS_TASK_EXIT=None -- --apply
assert_rc 1
assert_desired laravel-octane 0
assert_secret_eq APP_KEY "$OLD_KEY"
done_case

run_outer outer_a_task_that_never_starts_changes_nothing FAKE_AWS_RUNTASK_NOARN=1 -- --apply
assert_rc 1
assert_secret_eq APP_KEY "$OLD_KEY"
assert_secret_lacks APP_KEY_NEXT
assert_desired laravel-octane 3
done_case

# "It could not run" is not "it failed". A waiter that gives up says so and
# refuses to guess, because the task may still be re-encrypting.
run_outer outer_a_task_that_never_stops_is_not_assumed_failed FAKE_AWS_RUNTASK_NOSTOP=1 -- --apply
assert_rc 1
assert_says "may STILL BE re-encrypting"
assert_secret_eq APP_KEY "$OLD_KEY"
assert_desired laravel-octane 0
done_case

# The desired count already lives in main.tf and in startup-sweep.sh's
# DESIRED table. This script restores what it FOUND — 3, not a hardcoded 2.
run_outer outer_restores_the_counts_it_found -- --apply
assert_rc 0
assert_desired laravel-octane 3
assert_desired laravel-horizon 1
assert_says "APP_KEY rotated"
done_case

run_outer outer_a_happy_path_ends_with_the_staging_keys_gone -- --apply
assert_rc 0
assert_secret_is_a_fresh_key
assert_secret_lacks APP_KEY_NEXT
assert_secret_lacks APP_KEY_ROTATION_RESTORE
done_case

# One service failing must not strand the others: a service left on the old
# key cannot read the ledger at all.
run_outer outer_one_service_failing_to_roll_does_not_stop_the_others \
  FAKE_AWS_UNSTABLE=laravel-horizon -- --apply
assert_rc 1
assert_aws_called "update-service .*laravel-reverb .*--force-new-deployment"
assert_says INCOMPLETE
assert_says "--finish"
# The staging keys survive an incomplete run, because --finish needs them.
assert_secret_has APP_KEY_NEXT
done_case

# The verification is a gate: if it cannot run, the rotation is not
# reported as verified.
run_outer outer_an_unverifiable_rotation_is_not_reported_as_verified \
  FAKE_AWS_VERIFY_EXIT=1 -- --apply
assert_rc 1
assert_says "could not verify"
assert_secret_has APP_KEY_NEXT
done_case

run_outer outer_apply_refuses_when_a_rotation_is_already_in_flight -- --apply
# Re-run --apply against the state the previous case left behind is not
# possible here (each case gets fresh state), so stage it explicitly.
CURRENT=outer_apply_refuses_when_a_rotation_is_already_in_flight
fresh_state
jq --arg k "$NEW_KEY" '.APP_KEY_NEXT = $k' < "${STATE}/secret.json" > "${STATE}/s2" && mv "${STATE}/s2" "${STATE}/secret.json"
OUT="$(env PATH="${WORK}/bin:${PATH}" FAKE_AWS_LOG="${STATE}/aws.log" FAKE_AWS_STATE="${STATE}" \
      ROTATE_SUBNETS=subnet-a ROTATE_SECURITY_GROUP=sg-a ROTATE_WAIT_TRIES=2 ROTATE_WAIT_INTERVAL=0 \
      bash "$OUTER" --apply 2>&1)"; RC=$?
assert_rc 1
assert_aws_not_called "create-db-snapshot"
assert_says "already in flight"
done_case

CURRENT=outer_finish_refuses_when_nothing_is_staged
fresh_state
OUT="$(env PATH="${WORK}/bin:${PATH}" FAKE_AWS_LOG="${STATE}/aws.log" FAKE_AWS_STATE="${STATE}" \
      ROTATE_SUBNETS=subnet-a ROTATE_SECURITY_GROUP=sg-a ROTATE_WAIT_TRIES=2 ROTATE_WAIT_INTERVAL=0 \
      bash "$OUTER" --finish 2>&1)"; RC=$?
assert_rc 1
assert_says "nothing staged"
done_case

# --finish needs NOTHING from the machine that started the rotation: both
# the key and the counts to restore come back out of Secrets Manager. This
# is the property the Azure script did not have (it needed ROTATE_NEWKEY
# exported from a 0600 file on the operator's laptop).
CURRENT=outer_finish_needs_nothing_but_the_secret
fresh_state
jq --arg k "$NEW_KEY" '.APP_KEY_NEXT = $k
   | .APP_KEY_ROTATION_RESTORE = "laravel-octane:3 laravel-horizon:1"' \
   < "${STATE}/secret.json" > "${STATE}/s2" && mv "${STATE}/s2" "${STATE}/secret.json"
printf '0' > "${STATE}/desired.laravel-octane"
printf '0' > "${STATE}/desired.laravel-horizon"
OUT="$(env PATH="${WORK}/bin:${PATH}" FAKE_AWS_LOG="${STATE}/aws.log" FAKE_AWS_STATE="${STATE}" \
      ROTATE_SUBNETS=subnet-a ROTATE_SECURITY_GROUP=sg-a ROTATE_WAIT_TRIES=2 ROTATE_WAIT_INTERVAL=0 \
      bash "$OUTER" --finish 2>&1)"; RC=$?
assert_rc 0
assert_aws_not_called "create-db-snapshot"   # the data is already rotated
assert_secret_eq APP_KEY "$NEW_KEY"
assert_secret_lacks APP_KEY_NEXT
assert_desired laravel-octane 3
assert_desired laravel-horizon 1
done_case

# Every recovery instruction the script prints names the task to go and
# read. `rc=$(run_rotation_task ...)` would run that function in a subshell
# and discard the ARN it captured, leaving a blank in the middle of the one
# message an operator reads at the worst moment.
run_outer outer_names_the_task_in_its_recovery_instructions FAKE_AWS_TASK_EXIT=20 -- --apply
assert_rc 1
assert_says "arn:aws:ecs:"
done_case

run_outer outer_names_the_task_when_the_waiter_gives_up FAKE_AWS_RUNTASK_NOSTOP=1 -- --apply
assert_rc 1
assert_says "arn:aws:ecs:"
done_case

# A key in scrollback is a key in a shell history and a CI log. The script
# never prints one; nothing below should ever change that.
CURRENT=outer_never_prints_a_key
run_outer outer_never_prints_a_key -- --apply
if grep -qE 'base64:[A-Za-z0-9+/=]{10,}' <<< "$OUT"; then
  fail_case "a key-shaped token reached the script's output"
fi
done_case

# Every secret write must go through file://, so the document never becomes
# an argv visible in `ps`. fake-aws exits 253 on any other form.
CURRENT=outer_writes_the_secret_by_file_never_by_argv
run_outer outer_writes_the_secret_by_file_never_by_argv -- --apply
assert_rc 0
assert_aws_called "put-secret-value .*file://"
assert_aws_not_called "put-secret-value .*base64:"
done_case

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
