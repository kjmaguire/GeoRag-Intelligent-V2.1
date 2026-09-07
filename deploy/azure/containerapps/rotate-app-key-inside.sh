#!/usr/bin/env bash
# rotate-app-key-inside.sh — the part of an APP_KEY rotation that has to run
# INSIDE the laravel-octane-cc replica. Shipped to the container by
# rotate-app-key.sh (base64 over `az containerapp exec`); do not run it by
# hand unless you are finishing a rotation that script started.
#
# Why one session, one replica: the plaintext dump lives on the container's
# filesystem, which is ephemeral and lost the moment a revision rolls. So the
# dump under the OLD key and the restore under the NEW key happen here, back
# to back, in the same replica, and only then does the outer script change
# the app's secret and roll a revision.
#
# Why not `php artisan audit:rotate-key`: its step 3 is `key:generate --force`,
# which writes the new key into .env — and the image ships no .env
# (.dockerignore excludes it; APP_KEY arrives as an env var from the Container
# App's secret). The command fails there, after the dump and before the
# restore. The manual sequence below mints the key with `--show` and hands it
# to the restore through the environment of that one process, which is the
# same in-process rebind the orchestrator does, without the file write.
#
# Why `down --status=200`: laravel-octane-cc has an HTTP liveness probe on
# /up. The default maintenance response is a 503 on every path; enough
# failed probes and Container Apps restarts the container — mid-rotation,
# with the dump on its disk. A 200 maintenance page keeps the probe green and
# still refuses real requests, so no audit row is written under the old key
# between the dump and the roll. (Horizon does not write query_audit_log;
# only the Octane query controller does, so pausing Horizon is unnecessary.)
#
# Output contract (last line, read by the outer script):
#   ROTATE_OK NEWKEY=base64:...
#   ROTATE_FAILED stage=<down|dump|mint|restore|shred> [dump=<path>]
# The new key is ALSO left at /tmp/secure/newkey (0600) so a rotation whose
# outer half failed can be finished with `rotate-app-key.sh --finish`.
set -u
umask 077

SECURE_DIR="${ROTATE_SECURE_DIR:-/tmp/secure}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="${SECURE_DIR}/audit-pii-${STAMP}.jsonl"

mkdir -p "$SECURE_DIR" && chmod 700 "$SECURE_DIR" || { echo "ROTATE_FAILED stage=prep"; exit 1; }

# 1. Maintenance mode, probe-safe.
if ! php artisan down --status=200 --retry=60 >/dev/null 2>&1; then
  echo "ROTATE_FAILED stage=down"
  exit 1
fi

# 2. Dump under the OLD key (the key this process booted with). Nothing has
#    changed yet, so on failure lift maintenance and stop.
if ! php artisan audit:dump-pii --output "$DUMP"; then
  php artisan up >/dev/null 2>&1
  echo "ROTATE_FAILED stage=dump"
  exit 1
fi

# 3. Mint. `--show` prints and writes nothing.
NEWKEY="$(php artisan key:generate --show 2>/dev/null | tr -d '\r' | grep -o 'base64:[A-Za-z0-9+/=]*' | head -1)"
if [ -z "$NEWKEY" ]; then
  php artisan up >/dev/null 2>&1
  echo "ROTATE_FAILED stage=mint dump=${DUMP}"
  exit 1
fi

# 4. Restore under the NEW key: the env override is what this one artisan
#    process boots with, so Crypt encrypts with it and the query_text_hash
#    mutator re-keys too. From here on the rows are readable only with
#    NEWKEY — maintenance stays ON and the dump stays on disk if anything
#    fails, because they are the recovery assets.
if ! APP_KEY="$NEWKEY" php artisan audit:restore-pii --input "$DUMP"; then
  printf '%s\n' "$NEWKEY" > "${SECURE_DIR}/newkey"
  echo "ROTATE_FAILED stage=restore dump=${DUMP}"
  exit 1
fi

# 5. Shred the plaintext; keep the key for --finish.
if ! shred -u "$DUMP" 2>/dev/null; then
  rm -f "$DUMP"
fi
printf '%s\n' "$NEWKEY" > "${SECURE_DIR}/newkey"

echo "ROTATE_OK NEWKEY=${NEWKEY}"
