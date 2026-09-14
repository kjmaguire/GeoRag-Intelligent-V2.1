#!/usr/bin/env bash
# rotate-app-key-inside.sh — the half of an APP_KEY rotation that runs
# inside a container, on the one-off `georag-app-key-rotation` task.
# Shipped there base64'd as a command override by rotate-app-key.sh; do not
# run it by hand.
#
# Successor to deploy/azure/containerapps/rotate-app-key-inside.sh, deleted
# with the Azure tree on 2026-09-08 (ADR-0022). Three of that script's
# responsibilities are gone, and each is gone because the AWS shape removed
# the constraint that created it — read them before assuming this is a
# thinner version of the same thing:
#
#   MAINTENANCE MODE. The Azure script ran in the live replica, so it had
#   to stop that replica serving writes, and had to do it with
#   `down --status=200` because the default 503 failed the liveness probe
#   and got the container restarted with the dump on its disk. Here the
#   serving tasks are scaled to ZERO by the outer script before this runs.
#   That is not a workaround for a missing maintenance mode; it is the only
#   correct option available. config/app.php:121 leaves
#   APP_MAINTENANCE_DRIVER at `file`, so maintenance state lives on one
#   container's filesystem — `php artisan down` in THIS task would reach
#   nothing, and laravel-octane runs two tasks. Scaling to zero is also the
#   only form of "stopped writing" that can be verified from outside
#   (runningCount), rather than inferred.
#
#   MINTING. The Azure script minted the key here with `key:generate
#   --show` and handed it back over stdout. Here the key is minted by the
#   operator script and injected as APP_KEY_NEXT (terraform/rotation.tf).
#   Nothing this task prints is ever secret, which matters more than it did
#   on Azure: this task's stdout goes to CloudWatch Logs, where it persists
#   and is readable by anyone with logs:FilterLogEvents. A key echoed here
#   would be worse than a key in scrollback, not better.
#
#   THE OUTPUT CONTRACT. `az containerapp exec` exited 0 for a successful
#   CONNECTION whatever the command did, so the Azure pair communicated
#   through a ROTATE_OK / ROTATE_FAILED line parsed out of the transcript.
#   `ecs describe-tasks` reports the container's real exit code, so the
#   exit code IS the contract here, and it is granular on purpose — the
#   outer script must distinguish "nothing changed" from "the ledger is
#   half-re-encrypted" WITHOUT reading logs. The ROTATE_* line is kept for
#   the human reading CloudWatch a year later, not for a parser.
#
# Exit codes:
#   0   done: every row re-encrypted under APP_KEY_NEXT, dump shredded
#   10  dump failed. NOTHING CHANGED. The old key still reads everything;
#       abandon the rotation and put nothing new in the secret.
#   20  restore failed. The ledger may be HALF re-encrypted. Do not roll
#       back the secret, and do not re-dump — see RECOVERY below.
#   30  restore succeeded but the plaintext dump could not be shredded.
#       The data is rotated; this is a hygiene failure, not a data one.
#   64  misuse (no APP_KEY_NEXT, or it is not a Laravel key).
#
# ---------------------------------------------------------------------
# RECOVERY FROM EXIT 20, WHICH IS THE ONE THAT MATTERS
# ---------------------------------------------------------------------
# audit:restore-pii is idempotent — it skips rows already carrying the
# incoming values and catches DecryptException to force-write the ones it
# cannot read (RestoreAuditPii.php:117-146) — so the cheap fix is simply to
# run it again against the same dump. This script does that once itself,
# before giving up, because a transient RDS blip should not cost anything
# heavier.
#
# If it still fails, the dump dies with this task and CANNOT be recreated:
# audit:dump-pii reads through the `encrypted` cast, so the first row that
# is already under the new key throws DecryptException, and DumpAuditPii's
# outer catch (line 186) fails the whole dump and unlinks the partial file.
# There is no key that reads a half-rotated table.
#
# That is why the outer script takes an RDS snapshot before starting this
# and refuses to proceed without one. The recovery for exit 20 is to
# restore that snapshot, with the OLD key — which is still AWSCURRENT in
# Secrets Manager, because the outer script promotes APP_KEY only after
# this task exits 0. It is a heavier recovery than Azure's (where the dump
# survived on the replica's disk and you re-ran the restore by hand), and
# unlike Azure's it always works.
set -u
umask 077

SECURE_DIR="${ROTATE_SECURE_DIR:-/tmp/secure}"
DUMP="${SECURE_DIR}/audit-pii-$(date -u +%Y%m%dT%H%M%SZ).jsonl"
ARTISAN="${ROTATE_ARTISAN:-php artisan}"

fail() { printf 'ROTATE_FAILED stage=%s\n' "$1" >&2; exit "$2"; }

# APP_KEY_NEXT is injected by the task definition, not passed in. An empty
# one means the operator script staged nothing, which would make the
# restore a no-op re-encryption under the CURRENT key — the rotation would
# report success and rotate nothing. Refuse before touching any data.
case "${APP_KEY_NEXT:-}" in
  base64:?*) ;;
  "") fail "prep APP_KEY_NEXT-empty" 64 ;;
  *)  fail "prep APP_KEY_NEXT-malformed" 64 ;;
esac

# The same value in both is the other way to rotate nothing while looking
# like a success. It means a previous rotation's promotion already ran.
if [ "${APP_KEY_NEXT}" = "${APP_KEY:-}" ]; then
  fail "prep APP_KEY_NEXT-equals-APP_KEY" 64
fi

mkdir -p "$SECURE_DIR" && chmod 700 "$SECURE_DIR" || fail prep 64

# --- 1. dump under the CURRENT key ------------------------------------------
# This is the task's injected APP_KEY, which is the key every row in
# query_audit_log is encrypted under right now. Nothing has changed yet, so
# a failure here costs nothing.
if ! $ARTISAN audit:dump-pii --output "$DUMP"; then
  rm -f "$DUMP"
  fail dump 10
fi

# --- 2. restore under APP_KEY_NEXT -------------------------------------------
# The env override is what this one artisan process boots with, so Crypt
# encrypts with it and the query_text_hash mutator re-keys too — the same
# in-process rebind `audit:rotate-key` does internally, without its
# `key:generate --force` step, which needs a .env the image does not ship
# (.dockerignore excludes it; APP_KEY arrives as an env var). That is why
# `audit:rotate-key` cannot be used here and this sequence exists at all.
#
# From the first written row onward the table is readable only with
# APP_KEY_NEXT. Everything below is recovery, not rollback.
restored=0
for attempt in 1 2; do
  if APP_KEY="$APP_KEY_NEXT" $ARTISAN audit:restore-pii --input "$DUMP"; then
    restored=1
    break
  fi
  if [ "$attempt" = 1 ]; then
    # Idempotent, so a second pass re-reads the same dump and force-writes
    # whatever the first pass did not reach. Worth one try before falling
    # to the snapshot.
    printf 'ROTATE_RETRY stage=restore attempt=2\n' >&2
    sleep 5
  fi
done
if [ "$restored" != 1 ]; then
  fail restore 20
fi

# --- 3. shred ----------------------------------------------------------------
# Fargate destroys this task's ephemeral storage when the task stops, so
# unlike the Azure replica there is no long-lived disk for the plaintext to
# linger on. Shred anyway, and report a failure to do so: "the platform
# will clean up" is a reason to be unworried, not a reason to skip it.
if ! shred -u "$DUMP" 2>/dev/null; then
  rm -f "$DUMP"
  if [ -e "$DUMP" ]; then
    fail shred 30
  fi
fi

echo "ROTATE_OK"
