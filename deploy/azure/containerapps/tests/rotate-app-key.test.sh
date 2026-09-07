#!/usr/bin/env bash
# Dry rehearsal of the APP_KEY rotation, against fakes instead of Azure and
# the Laravel image. No credentials, no network, no PHP. The 2026-09-06
# rehearsal could not run against a staging environment (there is none —
# the only Azure resource group is production), so this pins every decision
# the two scripts make instead:
#
#   inside_*     rotate-app-key-inside.sh with a fake `php artisan`:
#                the restore must run with APP_KEY set to the minted key,
#                a dump failure must lift maintenance, a restore failure
#                must NOT (the dump and key are the recovery assets)
#   outer_*      rotate-app-key.sh with a fake `az` + fake `script`:
#                dry run mutates nothing, preflight refuses a stopped
#                Postgres / two replicas / a literal APP_KEY, a failed
#                in-replica half sets no secret, one app failing to roll
#                does not stop the others, the key is written 0600 before
#                any secret changes, --finish reads the replica's copy
#
# Usage: bash deploy/azure/containerapps/tests/rotate-app-key.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTER="$(dirname "$HERE")/rotate-app-key.sh"
INSIDE="$(dirname "$HERE")/rotate-app-key-inside.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "${WORK}/bin" "${WORK}/home"

# ---------------------------------------------------------------- fakes ---
# `script -qec CMD /dev/null` → just run CMD.
cat > "${WORK}/bin/script" <<'FAKE'
#!/usr/bin/env bash
cmd=""
while [ $# -gt 0 ]; do
  case "$1" in -qec|-qc|-ec|-c) cmd="$2"; shift 2 ;; *) shift ;; esac
done
exec bash -c "$cmd"
FAKE

# `az` — logs every call; behaviour from FAKE_* env vars.
cat > "${WORK}/bin/az" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_AZ_LOG:?}"
arg_after() { local want="$1" prev="" a; shift; for a in "$@"; do [ "$prev" = "$want" ] && { printf '%s' "$a"; return 0; }; prev="$a"; done; return 1; }
in_list() { local n="$1" i; shift; for i in $*; do [ "$i" = "$n" ] && return 0; done; return 1; }
has() { printf '%s' "$2" | grep -qF -- "$1"; }
app=$(arg_after -n "$@")
case "$1 ${2:-}" in
  "postgres flexible-server") printf '%s\n' "${FAKE_PG_STATE:-Ready}"; exit 0 ;;
  "containerapp replica")     printf '%s\n' "${FAKE_REPLICAS:-1}"; exit 0 ;;
  "containerapp show")
    if in_list "$app" "${FAKE_MISSING_APPS:-}"; then echo "ERROR: (ResourceNotFound)" >&2; exit 3; fi
    if printf '%s' "$*" | grep -q "secretRef"; then
      if in_list "$app" "${FAKE_LITERAL_APPS:-}"; then exit 0; fi
      if in_list "$app" "${FAKE_NO_APPKEY_APPS:-}"; then exit 0; fi
      printf 'app-key\n'; exit 0
    fi
    if printf '%s' "$*" | grep -q "\.value"; then
      if in_list "$app" "${FAKE_LITERAL_APPS:-}"; then printf 'base64:literal\n'; fi
      exit 0
    fi
    if printf '%s' "$*" | grep -q latestRevisionName; then printf '%s--rot1\n' "$app"; exit 0; fi
    exit 0 ;;
  "containerapp job")
    case "${3:-}" in
      show) if [ "${FAKE_NO_JOB:-0}" = 1 ]; then exit 3; fi; printf '%s' "$*" | grep -q secretRef && printf 'app-key\n'; exit 0 ;;
      secret) exit "${FAKE_JOB_SECRET_RC:-0}" ;;
    esac; exit 0 ;;
  "containerapp exec")
    cmd=$(arg_after --command "$@")
    if has "cat /tmp/secure/newkey" "$cmd"; then printf '%s\n' "${FAKE_REPLICA_KEY:-base64:cmVwbGljYS1rZXktZnJvbS10bXAtc2VjdXJlLW5ld2tleQ==}"; exit 0; fi
    if has "tinker" "$cmd"; then printf '%s\n' "${FAKE_VERIFY:-DECRYPT_OK}"; exit 0; fi
    printf '%s\n' "${FAKE_EXEC_OUTPUT:-ROTATE_OK NEWKEY=base64:bWludGVkLWluc2lkZS10aGUtcmVwbGljYS0wMDAwMDAwMDA=}"; exit 0 ;;
  "containerapp secret")
    if in_list "$app" "${FAKE_FAIL_SECRET_APPS:-}"; then echo "ERROR: fake secret failure" >&2; exit 3; fi; exit 0 ;;
  "containerapp update")
    if in_list "$app" "${FAKE_FAIL_ROLL_APPS:-}"; then echo "ERROR: fake roll failure" >&2; exit 3; fi; exit 0 ;;
  "containerapp revision")
    if printf '%s' "$*" | grep -q runningState; then
      in_list "$app" "${FAKE_UNHEALTHY_APPS:-}" && printf 'ActivationFailed\n' || printf 'Running\n'
    else
      in_list "$app" "${FAKE_UNHEALTHY_APPS:-}" && printf 'Unhealthy\n' || printf 'Healthy\n'
    fi; exit 0 ;;
esac
exit 0
FAKE

# `php artisan ...` — the in-replica commands. Records the APP_KEY it saw.
cat > "${WORK}/bin/php" <<'FAKE'
#!/usr/bin/env bash
printf 'APP_KEY=%s :: %s\n' "${APP_KEY:-<unset>}" "$*" >> "${FAKE_PHP_LOG:?}"
shift  # artisan
case "$1 ${2:-}" in
  "down --status=200") exit "${FAKE_DOWN_RC:-0}" ;;
  "up ") exit 0 ;;
  "audit:dump-pii --output")
    [ "${FAKE_DUMP_RC:-0}" -ne 0 ] && { echo "dump failed (fake)"; exit "${FAKE_DUMP_RC}"; }
    printf '{"audit_id":1}\n{"__meta__":true}\n' > "$3"; echo "Dumped 1 rows to $3"; exit 0 ;;
  "key:generate --show") printf '%s\n' "${FAKE_MINT_KEY-base64:bWludGVkLWZha2Uta2V5LTAwMDAwMDAwMDAwMDAwMDAwMDA=}"; exit "${FAKE_MINT_RC:-0}" ;;
  "audit:restore-pii --input")
    [ -f "$3" ] || { echo "no dump at $3"; exit 1; }
    [ "${FAKE_RESTORE_RC:-0}" -ne 0 ] && { echo "restore failed (fake)"; exit "${FAKE_RESTORE_RC}"; }
    echo "Integrity check passed"; exit 0 ;;
esac
exit 0
FAKE

cat > "${WORK}/bin/shred" <<'FAKE'
#!/usr/bin/env bash
printf 'shred %s\n' "$*" >> "${FAKE_PHP_LOG:?}"
for f in "$@"; do case "$f" in -*) ;; *) rm -f "$f" ;; esac; done
FAKE
chmod +x "${WORK}/bin/"*

PASS=0; FAIL=0; CURRENT=""; OUT=""; RC=0
fail_case() { printf '  FAIL  %s: %s\n' "$CURRENT" "$1"; FAIL=$((FAIL + 1)); }
assert_rc() { [ "$RC" -eq "$1" ] && return 0; fail_case "expected exit ${1}, got ${RC}"; sed 's/^/        /' <<< "$OUT"; }
assert_says() { grep -qF -- "$1" <<< "$OUT" || fail_case "output does not mention: $1"; }
assert_silent_about() { grep -qF -- "$1" <<< "$OUT" && fail_case "output should not mention: $1"; return 0; }
assert_az_calls() { local n; n=$(grep -cE -- "$1" "${WORK}/az.log" 2>/dev/null || true); [ "${n:-0}" -eq "$2" ] || fail_case "expected ${2} az calls matching /${1}/, saw ${n:-0}"; }
assert_php_log() { grep -qE -- "$1" "${WORK}/php.log" || fail_case "php log lacks /${1}/"; }
assert_php_log_not() { grep -qE -- "$1" "${WORK}/php.log" && fail_case "php log should lack /${1}/"; return 0; }
check() { local before=$FAIL; "$@"; [ "$FAIL" -eq "$before" ] && PASS=$((PASS + 1)); }

run_inside() {
  CURRENT="$1"; shift
  : > "${WORK}/php.log"; rm -rf "${WORK}/secure"
  OUT=$(env -i PATH="${WORK}/bin:/usr/bin:/bin" FAKE_PHP_LOG="${WORK}/php.log" ROTATE_SECURE_DIR="${WORK}/secure" "$@" bash "$INSIDE" 2>&1); RC=$?
  printf '  ....  %s\n' "$CURRENT"
}
# run_outer <case> [VAR=VALUE ...] [-- <script args>]
run_outer() {
  CURRENT="$1"; shift
  local envs=() args=()
  while [ $# -gt 0 ]; do
    if [ "$1" = "--" ]; then shift; args=("$@"); break; fi
    envs+=("$1"); shift
  done
  : > "${WORK}/az.log"; : > "${WORK}/php.log"; rm -rf "${WORK}/home"; mkdir -p "${WORK}/home"
  OUT=$(env -i PATH="${WORK}/bin:/usr/bin:/bin" HOME="${WORK}/home" FAKE_AZ_LOG="${WORK}/az.log" FAKE_PHP_LOG="${WORK}/php.log" \
        ROTATE_KEY_FILE="${WORK}/home/key.txt" ROTATE_WAIT_TRIES=1 ROTATE_WAIT_INTERVAL=0 \
        ${envs[@]+"${envs[@]}"} bash "$OUTER" ${args[@]+"${args[@]}"} 2>&1); RC=$?
  printf '  ....  %s\n' "$CURRENT"
}

echo "rotate-app-key-inside.sh"

check_inside_happy() {
  run_inside inside_happy
  assert_rc 0
  assert_says "ROTATE_OK NEWKEY=base64:bWludGVkLWZha2Uta2V5"
  assert_php_log "APP_KEY=<unset> :: artisan down --status=200 --retry=60"
  assert_php_log "APP_KEY=<unset> :: artisan audit:dump-pii --output"
  # The restore ran with the minted key in its environment — the whole trick.
  assert_php_log "APP_KEY=base64:bWludGVkLWZha2Uta2V5[^ ]* :: artisan audit:restore-pii --input"
  assert_php_log "^shred -u .*audit-pii-"
  [ -f "${WORK}/secure/newkey" ] || fail_case "newkey not left for --finish"
  [ "$(stat -c %a "${WORK}/secure/newkey")" = "600" ] || fail_case "newkey not 0600"
  [ -z "$(ls "${WORK}/secure"/audit-pii-* 2>/dev/null)" ] || fail_case "plaintext dump survived"
  # Maintenance stays on: the outer script's roll replaces the replica.
  assert_php_log_not ":: artisan up"
}
check check_inside_happy

check_inside_dump_fails() {
  run_inside inside_dump_fails FAKE_DUMP_RC=2
  assert_rc 1
  assert_says "ROTATE_FAILED stage=dump"
  assert_php_log ":: artisan up"                     # nothing changed → lift maintenance
  assert_php_log_not "audit:restore-pii"
  assert_silent_about "NEWKEY="
}
check check_inside_dump_fails

check_inside_mint_fails() {
  run_inside inside_mint_fails FAKE_MINT_KEY=""
  assert_rc 1
  assert_says "ROTATE_FAILED stage=mint"
  assert_php_log ":: artisan up"
  assert_php_log_not "audit:restore-pii"
}
check check_inside_mint_fails

check_inside_restore_fails() {
  run_inside inside_restore_fails FAKE_RESTORE_RC=1
  assert_rc 1
  assert_says "ROTATE_FAILED stage=restore dump="
  assert_php_log_not ":: artisan up"                 # data half-rotated → stay in maintenance
  [ -n "$(ls "${WORK}/secure"/audit-pii-* 2>/dev/null)" ] || fail_case "dump was not preserved"
  [ -f "${WORK}/secure/newkey" ] || fail_case "newkey not preserved for recovery"
  assert_silent_about "ROTATE_OK"
}
check check_inside_restore_fails

echo "rotate-app-key.sh"

check_outer_dry_run() {
  run_outer outer_dry_run
  assert_rc 0
  assert_says "dry run. Re-run with --apply."
  assert_says "laravel-octane-cc  (secret app-key)"
  assert_says "laravel-migrate-job  (job secret app-key)"
  assert_az_calls "^containerapp exec" 0
  assert_az_calls "^containerapp secret set" 0
  assert_az_calls "^containerapp update" 0
  [ ! -f "${WORK}/home/key.txt" ] || fail_case "dry run wrote a key file"
}
check check_outer_dry_run

check_outer_pg_stopped() {
  run_outer outer_pg_stopped FAKE_PG_STATE=Stopped -- --apply
  assert_rc 1
  assert_says "not Ready"
  assert_az_calls "^containerapp exec" 0
}
check check_outer_pg_stopped

check_outer_two_replicas() {
  run_outer outer_two_replicas FAKE_REPLICAS=2 -- --apply
  assert_rc 1
  assert_says "has 2 running replicas"
  assert_az_calls "^containerapp exec" 0
}
check check_outer_two_replicas

check_outer_literal_app_key() {
  run_outer outer_literal_app_key FAKE_LITERAL_APPS=laravel-horizon-cc -- --apply
  assert_rc 1
  assert_says "laravel-horizon-cc carries APP_KEY as a literal env value"
  assert_az_calls "^containerapp exec" 0
}
check check_outer_literal_app_key

check_outer_happy() {
  run_outer outer_happy -- --apply
  assert_rc 0
  assert_says "APP_KEY rotated on laravel-octane-cc laravel-horizon-cc laravel-reverb-cc and laravel-migrate-job"
  assert_says "decrypt verified"
  assert_az_calls "^containerapp exec" 2                 # rotation + verify
  assert_az_calls "^containerapp secret set" 3
  assert_az_calls "^containerapp job secret set" 1
  assert_az_calls "^containerapp update .* --revision-suffix" 3
  # The key never reaches the terminal, but is kept 0600 for the operator.
  assert_silent_about "bWludGVkLWluc2lkZS10aGUtcmVwbGljYS"
  grep -q "bWludGVkLWluc2lkZS10aGUtcmVwbGljYS" "${WORK}/home/key.txt" || fail_case "key file lacks the minted key"
  [ "$(stat -c %a "${WORK}/home/key.txt")" = "600" ] || fail_case "key file not 0600"
  # The secret set carried the minted key, not a placeholder.
  grep -q "secret set .* --secrets app-key=base64:bWludGVkLWluc2lkZS10aGUtcmVwbGljYS" "${WORK}/az.log" || fail_case "secret set did not carry the minted key"
}
check check_outer_happy

check_outer_inside_failed() {
  run_outer outer_inside_failed "FAKE_EXEC_OUTPUT=ROTATE_FAILED stage=dump" -- --apply
  assert_rc 1
  assert_says "FAILED inside laravel-octane-cc: ROTATE_FAILED stage=dump"
  assert_says "Nothing changed; maintenance mode was lifted."
  assert_az_calls "^containerapp secret set" 0
  assert_az_calls "^containerapp update" 0
  [ ! -f "${WORK}/home/key.txt" ] || fail_case "key file written despite failure"
}
check check_outer_inside_failed

check_outer_restore_failed_inside() {
  run_outer outer_restore_failed_inside "FAKE_EXEC_OUTPUT=ROTATE_FAILED stage=restore dump=/tmp/secure/x.jsonl" -- --apply
  assert_rc 1
  assert_says "Do not roll the revision"
  assert_az_calls "^containerapp secret set" 0
}
check check_outer_restore_failed_inside

check_outer_no_verdict() {
  run_outer outer_no_verdict "FAKE_EXEC_OUTPUT=termios.error: (25, 'Inappropriate ioctl for device')" -- --apply
  assert_rc 1
  assert_says "no ROTATE_* verdict"
  assert_az_calls "^containerapp secret set" 0
}
check check_outer_no_verdict

check_outer_one_roll_fails() {
  run_outer outer_one_roll_fails FAKE_FAIL_ROLL_APPS=laravel-reverb-cc -- --apply
  assert_rc 1
  assert_says "FAILED: could not roll a new revision of laravel-reverb-cc"
  assert_says "rotation INCOMPLETE: 1 step(s) failed"
  assert_says "finish with --finish"
  # The other apps were still done; the migrate job too.
  assert_az_calls "^containerapp secret set" 3
  assert_az_calls "^containerapp update .* --revision-suffix" 3
  assert_az_calls "^containerapp job secret set" 1
  [ -f "${WORK}/home/key.txt" ] || fail_case "key file missing after partial failure"
}
check check_outer_one_roll_fails

check_outer_unhealthy_revision() {
  run_outer outer_unhealthy_revision FAKE_UNHEALTHY_APPS=laravel-octane-cc -- --apply
  assert_rc 1
  assert_says "laravel-octane-cc laravel-octane-cc--rot1 healthState='Unhealthy' runningState='ActivationFailed'"
  assert_says "Do not put the old key back"
}
check check_outer_unhealthy_revision

check_outer_verify_fails() {
  run_outer outer_verify_fails FAKE_VERIFY="DecryptException: The MAC is invalid." -- --apply
  assert_rc 1
  assert_says "could not read an audit row"
  assert_says "does not match the key the rows were restored under"
}
check check_outer_verify_fails

check_outer_finish() {
  run_outer outer_finish -- --finish
  assert_rc 0
  assert_says "--finish: reading the key this replica minted earlier"
  assert_az_calls "^containerapp exec .*cat /tmp/secure/newkey" 1
  assert_az_calls "^containerapp secret set" 3
  grep -q "cmVwbGljYS1rZXktZnJvbS10bXAtc2VjdXJlLW5ld2tleQ" "${WORK}/home/key.txt" || fail_case "--finish did not use the replica's key"
  assert_silent_about "cmVwbGljYS1rZXktZnJvbS10bXAtc2VjdXJlLW5ld2tleQ"
}
check check_outer_finish

check_outer_finish_key_gone() {
  run_outer outer_finish_key_gone FAKE_REPLICA_KEY="cat: /tmp/secure/newkey: No such file or directory" -- --finish
  assert_rc 1
  assert_says "/tmp/secure/newkey is not readable"
  assert_az_calls "^containerapp secret set" 0
}
check check_outer_finish_key_gone

check_outer_finish_env_override() {
  run_outer outer_finish_env_override FAKE_REPLICA_KEY="cat: no" ROTATE_NEWKEY=base64:ZnJvbS10aGUtb3BlcmF0b3JzLWtleS1maWxl -- --finish
  # ROTATE_NEWKEY wins even though the replica has nothing: the operator's file copy.
  assert_rc 0
  assert_az_calls "^containerapp secret set" 3
}
check check_outer_finish_env_override

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
