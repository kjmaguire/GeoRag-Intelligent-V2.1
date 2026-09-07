#!/usr/bin/env bash
# rotate-app-key.sh — rotate Laravel's APP_KEY on Azure Container Apps.
#
#   bash deploy/azure/containerapps/rotate-app-key.sh            # dry run: preflight + plan
#   bash deploy/azure/containerapps/rotate-app-key.sh --apply    # do it
#   bash deploy/azure/containerapps/rotate-app-key.sh --finish   # secrets + roll only, after a
#                                                                # run that re-encrypted the data
#                                                                # but failed before every app
#                                                                # had the new secret
#
# APP_KEY encrypts query_audit_log.query_text / response_text and keys
# query_text_hash (docs/RUNBOOK.md § "APP_KEY rotation checklist"). Rotating
# it is dump-with-old-key → mint → restore-with-new-key → point every Laravel
# app at the new key. The first three run inside the single laravel-octane-cc
# replica (rotate-app-key-inside.sh, sent over `az containerapp exec`); this
# script does the preflight, ships that, then sets the secret on every app
# that reads it and rolls a new revision of each.
#
# Where the key goes. The value transits stdout of the exec session ONCE (as
# `php artisan key:generate --show` would), is captured into a variable, and
# is never placed on a command line except the unavoidable
# `az containerapp secret set`. It is ALSO written, 0600, to
# $ROTATE_KEY_FILE (default ~/.config/georag/app-key-rotation-<stamp>.txt)
# before any secret is changed. That is deliberate and unlike
# rotate-martin-credential.sh: once the restore has run, this key is the
# only thing that can read the audit rows, and a script that held the sole
# copy in a variable would turn a lost SSH session into lost data. Move it
# to the password manager and shred the file when the rotation is verified.
#
# Findings from the 2026-09-06 dry rehearsal that shaped this:
#   - `audit:rotate-key` is unusable here: its `key:generate --force` needs a
#     .env, and the image has none. Hence the manual sequence inside.
#   - `php artisan down` answers 503 to the /up liveness probe; enough of
#     those and the platform restarts the replica with the dump on it. The
#     inside script uses `down --status=200`.
#   - `az containerapp exec` dies without a TTY (termios error 25) and exits 0
#     regardless of what the command did — same as CD's smoke step. Every
#     exec here is wrapped in `script -qec` and judged on its output.
#   - Only the Octane query controller writes query_audit_log; Horizon does
#     not, so it is not paused. laravel-horizon-cc still gets the new secret
#     because it decrypts rows it reads.
#   - The roll is the point of no return for the OLD key: after it, the old
#     replica (and its /tmp/secure) is gone. --finish exists for the window
#     between restore and a complete roll.
set -uo pipefail
set +x   # never trace: the new key is in variables below

RG="${RG:-georag}"
OCTANE="${OCTANE_APP:-laravel-octane-cc}"
# Every app / job whose APP_KEY env reads a secret gets the new value. The
# list is the candidates; preflight keeps only the ones that actually
# reference a secret, and refuses if an app holds APP_KEY as a literal.
CANDIDATE_APPS="${ROTATE_APPS:-laravel-octane-cc laravel-horizon-cc laravel-reverb-cc}"
MIGRATE_JOB="${MIGRATE_JOB:-laravel-migrate-job}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSIDE="${HERE}/rotate-app-key-inside.sh"
KEY_FILE="${ROTATE_KEY_FILE:-${HOME}/.config/georag/app-key-rotation-$(date -u +%Y%m%dT%H%M%SZ).txt}"
WAIT_TRIES="${ROTATE_WAIT_TRIES:-20}"
WAIT_INTERVAL="${ROTATE_WAIT_INTERVAL:-10}"

MODE=dry
case "${1:-}" in
  --apply)  MODE=apply ;;
  --finish) MODE=finish ;;
  "")       MODE=dry ;;
  *) echo "usage: $0 [--apply|--finish]" >&2; exit 2 ;;
esac

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "ABORT: $1 is not on PATH. $2" >&2; exit 1; }
}
need az "Install the Azure CLI."
need script "util-linux's script(1) fakes the TTY that az containerapp exec insists on."
[ -f "$INSIDE" ] || { echo "ABORT: ${INSIDE} not found next to this script." >&2; exit 1; }

strip_cr() { tr -d '\r'; }
# Anything echoed from an exec session goes through this: the only base64:
# token that can appear there is a key.
redact() { sed -E 's/base64:[A-Za-z0-9+\/=]+/base64:<redacted>/g'; }

# exec_octane <shell command> — runs it in the live replica under a fake TTY
# and returns the output. The az exit code is discarded on purpose: it is 0
# for a successful CONNECTION whatever the command did.
exec_octane() {
  script -qec "az containerapp exec -g $RG -n $OCTANE --command \"$1\"" /dev/null 2>&1 | strip_cr
}

# --- preflight -------------------------------------------------------------
state=$(az postgres flexible-server show -g "$RG" -n georag-pg-cc --query state -o tsv 2>/dev/null | strip_cr)
if [ "$state" != "Ready" ]; then
  echo "ABORT: georag-pg-cc is '${state}', not Ready. The rotation rewrites every audit row;" >&2
  echo "it is stopped 06:00-14:00 UTC by the nightly saver. Wait, or start it." >&2
  exit 1
fi

if ! az containerapp show -g "$RG" -n "$OCTANE" >/dev/null 2>&1; then
  echo "ABORT: ${OCTANE} does not exist." >&2
  exit 1
fi

REPLICAS=$(az containerapp replica list -g "$RG" -n "$OCTANE" --query "length(@)" -o tsv 2>/dev/null | strip_cr)
if [ "${REPLICAS:-0}" != "1" ]; then
  echo "ABORT: ${OCTANE} has ${REPLICAS:-0} running replicas; this procedure needs exactly one." >&2
  echo "A second replica would keep writing rows under the old key while the first is in maintenance." >&2
  exit 1
fi

# Which secret holds APP_KEY, and on which apps. Discover, never assume.
APPS=""
SECRET_NAME=""
for app in $CANDIDATE_APPS; do
  if ! az containerapp show -g "$RG" -n "$app" >/dev/null 2>&1; then
    echo "note: ${app} does not exist; skipping." >&2
    continue
  fi
  ref=$(az containerapp show -g "$RG" -n "$app" \
        --query "properties.template.containers[0].env[?name=='APP_KEY'].secretRef | [0]" -o tsv 2>/dev/null | strip_cr)
  lit=$(az containerapp show -g "$RG" -n "$app" \
        --query "properties.template.containers[0].env[?name=='APP_KEY'].value | [0]" -o tsv 2>/dev/null | strip_cr)
  if [ -z "$ref" ] && [ -n "$lit" ]; then
    echo "ABORT: ${app} carries APP_KEY as a literal env value, not a secretRef." >&2
    echo "Convert it to a secret first (az containerapp secret set + --set-env-vars APP_KEY=secretref:<name>)." >&2
    exit 1
  fi
  if [ -z "$ref" ]; then
    echo "note: ${app} has no APP_KEY env; skipping." >&2
    continue
  fi
  if [ -n "$SECRET_NAME" ] && [ "$ref" != "$SECRET_NAME" ]; then
    echo "note: ${app} reads APP_KEY from secret '${ref}' (others use '${SECRET_NAME}'); both will be set." >&2
  fi
  [ -z "$SECRET_NAME" ] && SECRET_NAME="$ref"
  APPS="${APPS} ${app}:${ref}"
done
if [ -z "$APPS" ]; then
  echo "ABORT: no app reads APP_KEY from a secret; nothing to rotate." >&2
  exit 1
fi
case " $APPS " in
  *" ${OCTANE}:"*) ;;
  *) echo "ABORT: ${OCTANE} is not among the apps reading APP_KEY; the rotation runs there." >&2; exit 1 ;;
esac

JOB_REF=""
if az containerapp job show -g "$RG" -n "$MIGRATE_JOB" >/dev/null 2>&1; then
  JOB_REF=$(az containerapp job show -g "$RG" -n "$MIGRATE_JOB" \
            --query "properties.template.containers[0].env[?name=='APP_KEY'].secretRef | [0]" -o tsv 2>/dev/null | strip_cr)
fi

if [ "$MODE" = dry ]; then
  cat >&2 <<PLAN
# dry run. Re-run with --apply.
#
# Preflight passed: georag-pg-cc Ready, ${OCTANE} has 1 replica.
# Apps reading APP_KEY from a secret:
PLAN
  for entry in $APPS; do echo "#   ${entry%%:*}  (secret ${entry#*:})" >&2; done
  [ -n "$JOB_REF" ] && echo "#   ${MIGRATE_JOB}  (job secret ${JOB_REF})" >&2
  cat >&2 <<PLAN
#
# It will:
#   1. inside ${OCTANE}: maintenance mode (200), dump audit PII under the old
#      key, mint a key, restore under the new key, shred the dump
#      (rotate-app-key-inside.sh)
#   2. write the new key 0600 to ${KEY_FILE}
#   3. set the secret on every app above (and the migrate job)
#   4. roll a new revision of each app and wait for Healthy
#   5. verify the newest revision can decrypt an audit row
#
# The key never appears on this terminal. psql is not needed.
PLAN
  exit 0
fi

# --- 1. inside the replica ---------------------------------------------------
NEWKEY=""
if [ "$MODE" = apply ]; then
  echo "# running the in-replica half on ${OCTANE} (maintenance → dump → mint → restore → shred)..." >&2
  B64=$(base64 -w0 < "$INSIDE" 2>/dev/null || base64 < "$INSIDE" | tr -d '\n')
  OUT=$(exec_octane "bash -c 'echo ${B64} | base64 -d | bash'")
  LAST=$(printf '%s\n' "$OUT" | grep -E '^ROTATE_(OK|FAILED)' | tail -1)
  case "$LAST" in
    "ROTATE_OK NEWKEY="*)
      NEWKEY="${LAST#ROTATE_OK NEWKEY=}"
      ;;
    "ROTATE_FAILED "*)
      echo "FAILED inside ${OCTANE}: ${LAST}" >&2
      printf '%s\n' "$OUT" | grep -v '^ROTATE_' | tail -20 | redact | sed 's/^/    /' >&2
      case "$LAST" in
        *stage=restore*)
          echo "The rows are now encrypted under the NEW key and maintenance mode is still on." >&2
          echo "The key is at /tmp/secure/newkey and the dump at the path above, INSIDE the replica." >&2
          echo "Do not roll the revision. Re-run the restore by hand in that replica, then --finish." >&2
          ;;
        *)
          echo "Nothing changed; maintenance mode was lifted." >&2
          ;;
      esac
      exit 1
      ;;
    *)
      echo "FAILED: no ROTATE_* verdict from the exec session. Output:" >&2
      printf '%s\n' "$OUT" | tail -20 | redact | sed 's/^/    /' >&2
      echo "Check whether the replica is in maintenance mode: az containerapp exec ... 'php artisan up'." >&2
      exit 1
      ;;
  esac
elif [ -n "${ROTATE_NEWKEY:-}" ]; then
  echo "# --finish: using ROTATE_NEWKEY from the environment (the operator's file copy)." >&2
  NEWKEY="$ROTATE_NEWKEY"
else
  echo "# --finish: reading the key this replica minted earlier..." >&2
  OUT=$(exec_octane "cat /tmp/secure/newkey")
  NEWKEY=$(printf '%s\n' "$OUT" | grep -o 'base64:[A-Za-z0-9+/=]*' | head -1)
  if [ -z "$NEWKEY" ]; then
    echo "FAILED: /tmp/secure/newkey is not readable in ${OCTANE}. If the replica restarted since the" >&2
    echo "rotation, the file is gone; the key is in ${ROTATE_KEY_FILE:-~/.config/georag/app-key-rotation-*.txt}" >&2
    echo "from the original run — export ROTATE_NEWKEY from it and re-run --finish." >&2
    exit 1
  fi
fi
case "$NEWKEY" in base64:*) ;; *) echo "FAILED: minted key has an unexpected shape." >&2; exit 1 ;; esac

# --- 2. keep a copy before touching anything ----------------------------------
mkdir -p "$(dirname "$KEY_FILE")" && chmod 700 "$(dirname "$KEY_FILE")"
( umask 077; printf '%s\n' "$NEWKEY" > "$KEY_FILE" ) || { echo "FAILED: could not write ${KEY_FILE}; refusing to continue without a copy of the key." >&2; exit 1; }
echo "# new key written 0600 to ${KEY_FILE} — move it to the password manager, shred the file when verified." >&2

# --- 3. secrets, 4. roll ------------------------------------------------------
FAILURES=0
for entry in $APPS; do
  app="${entry%%:*}"; ref="${entry#*:}"
  if ! az containerapp secret set -g "$RG" -n "$app" --secrets "${ref}=${NEWKEY}" --output none 2>/dev/null; then
    echo "FAILED: secret set on ${app}" >&2; FAILURES=$((FAILURES + 1)); continue
  fi
  if ! az containerapp update -g "$RG" -n "$app" --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none 2>/dev/null; then
    echo "FAILED: could not roll a new revision of ${app}" >&2; FAILURES=$((FAILURES + 1)); continue
  fi
  echo "# ${app}: secret set, revision rolling" >&2
done
if [ -n "$JOB_REF" ]; then
  if ! az containerapp job secret set -g "$RG" -n "$MIGRATE_JOB" --secrets "${JOB_REF}=${NEWKEY}" --output none 2>/dev/null; then
    echo "FAILED: secret set on ${MIGRATE_JOB}" >&2; FAILURES=$((FAILURES + 1))
  fi
fi
unset NEWKEY

for entry in $APPS; do
  app="${entry%%:*}"
  LATEST=$(az containerapp show -g "$RG" -n "$app" --query "properties.latestRevisionName" -o tsv 2>/dev/null | strip_cr)
  health=""; running=""
  i=0
  while [ "$i" -lt "$WAIT_TRIES" ]; do
    health=$(az containerapp revision show -g "$RG" -n "$app" --revision "$LATEST" --query "properties.healthState" -o tsv 2>/dev/null | strip_cr)
    running=$(az containerapp revision show -g "$RG" -n "$app" --revision "$LATEST" --query "properties.runningState" -o tsv 2>/dev/null | strip_cr)
    [ "$health" = "Healthy" ] && break
    [ "$running" = "ActivationFailed" ] && break
    i=$((i + 1)); sleep "$WAIT_INTERVAL"
  done
  if [ "$health" != "Healthy" ]; then
    echo "FAILED: ${app} ${LATEST} healthState='${health:-unknown}' runningState='${running:-unknown}'" >&2
    echo "  az containerapp logs show -g $RG -n $app --tail 50" >&2
    FAILURES=$((FAILURES + 1))
  else
    echo "# ${app}: ${LATEST} Healthy" >&2
  fi
done

# --- 5. verify ----------------------------------------------------------------
VERIFY=$(exec_octane "php artisan tinker --execute 'echo App\\\\Models\\\\QueryAuditLog::query()->latest()->first()?->query_text !== null ? \"DECRYPT_OK\" : \"NO_ROWS\";'")
case "$VERIFY" in
  *DECRYPT_OK*) echo "# verify: the new ${OCTANE} revision decrypts audit rows." >&2 ;;
  *NO_ROWS*)    echo "# verify: query_audit_log is empty; nothing to decrypt (fine on a fresh install)." >&2 ;;
  *)
    echo "FAILED: the new revision could not read an audit row. Output:" >&2
    printf '%s\n' "$VERIFY" | tail -10 | redact | sed 's/^/    /' >&2
    echo "The secret the app boots with does not match the key the rows were restored under." >&2
    echo "Set it from ${KEY_FILE} and roll again (--finish with ROTATE_NEWKEY exported)." >&2
    FAILURES=$((FAILURES + 1))
    ;;
esac

if [ "$FAILURES" -eq 0 ]; then
  echo "APP_KEY rotated on $(for e in $APPS; do printf '%s ' "${e%%:*}"; done)${JOB_REF:+and ${MIGRATE_JOB}}; new revisions Healthy; decrypt verified." >&2
  echo "Next: update .env.production.enc, log it to authz_audit (secret-rotation.md § 16), shred ${KEY_FILE}." >&2
  exit 0
fi
echo "APP_KEY rotation INCOMPLETE: ${FAILURES} step(s) failed. The data is under the new key; finish with --finish" >&2
echo "(ROTATE_NEWKEY from ${KEY_FILE} if the replica is gone). Do not put the old key back." >&2
exit 1
