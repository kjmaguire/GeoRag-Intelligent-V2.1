#!/usr/bin/env bash
# Apply deploy/azure/containerapps/redis.yaml to redis-cc, safely.
#
#   bash deploy/azure/containerapps/apply-redis.sh            # show
#   bash deploy/azure/containerapps/apply-redis.sh --apply    # do it
#
# Runs from Git Bash, WSL or native bash as-is. Unlike its sibling
# create-alerts.sh it needs no MSYS_NO_PATHCONV, and it is unharmed if you
# have set one -- see the cygpath conversion further down for why that
# matters.
#
# ---------------------------------------------------------------------
# WHY THIS SCRIPT EXISTS INSTEAD OF A ONE-LINE az COMMAND
# ---------------------------------------------------------------------
# redis.yaml carries the shape Container Apps requires, which includes a
# `secrets:` block. Its value is the placeholder REPLACE_AT_DEPLOY_TIME,
# because the real password is not in git. Running
#
#     az containerapp update -g georag -n redis-cc --yaml redis.yaml
#
# sends that block as-is, which sets the live `redis-password` secret to
# the literal string REPLACE_AT_DEPLOY_TIME. Every client -- Laravel
# cache, sessions, Horizon queues, and the FastAPI tier -- then fails
# auth against a Redis that is otherwise perfectly healthy. The manifest
# header used to name that exact command as the way to deploy it.
#
# So the copy this script sends has the secrets block removed. Omitting
# the block leaves the existing secret untouched; providing it replaces
# it. That difference is the entire point of the script, and the
# verification step at the end checks the secret is still there rather
# than trusting the claim.
set -uo pipefail

RG=georag
APP=redis-cc
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${HERE}/redis.yaml"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

STRIPPED="$(mktemp -t redis-apply-XXXXXX.yaml)"
trap 'rm -f "$STRIPPED"' EXIT

# Drop the `secrets:` mapping and everything nested under it, by
# indentation. Nothing else in the file is touched.
awk '
  /^[[:space:]]*secrets:[[:space:]]*$/ {
    match($0, /^[[:space:]]*/); depth = RLENGTH; skipping = 1; next
  }
  skipping {
    if ($0 ~ /^[[:space:]]*$/) { next }
    match($0, /^[[:space:]]*/)
    if (RLENGTH > depth) { next }
    skipping = 0
  }
  { print }
' "$SOURCE" > "$STRIPPED"

# Comments are not payload.
#
# This guard used to grep the whole stripped file for the placeholder, and
# redis.yaml's own header comment is a paragraph explaining what the
# placeholder is and why sending it would break every Redis client. So the
# check matched that comment on every single run and the script aborted
# before doing anything -- in dry run and under --apply alike. It had never
# got past this point.
payload="$(grep -v '^[[:space:]]*#' "$STRIPPED")"

if printf '%s
' "$payload" | grep -q "REPLACE_AT_DEPLOY_TIME"; then
  echo "ABORT: the placeholder survived the strip -- refusing to send it." >&2
  exit 1
fi

# The invariant that actually matters, checked directly rather than through
# a magic string: no secrets MAPPING in what we send. A `secretRef:` further
# down the template is a REFERENCE to the live secret and must survive --
# stripping that would be a different bug with the same symptom.
if printf '%s
' "$payload" | grep -qE '^[[:space:]]*secrets:[[:space:]]*$'; then
  echo "ABORT: the secrets mapping is still present -- refusing to send it." >&2
  exit 1
fi

if ! grep -q "maxmemory" "$STRIPPED"; then
  echo "ABORT: the strip removed too much (no redis-server args left)." >&2
  exit 1
fi

echo "# --- what is live now ---" >&2
az containerapp show -g "$RG" -n "$APP" \
  --query "{cpu:properties.template.containers[0].resources.cpu,memory:properties.template.containers[0].resources.memory,args:properties.template.containers[0].args[0]}" \
  -o yaml >&2 2>/dev/null

echo >&2
echo "# --- what this would set ---" >&2
grep -E "^\s+(--|cpu:|memory:)" "$STRIPPED" >&2

if [ "$APPLY" -eq 0 ]; then
  echo >&2
  echo "# dry run. Re-run with --apply." >&2
  echo "# The command it will run:" >&2
  echo "#   az containerapp update -g $RG -n $APP --yaml <secrets-stripped copy>" >&2
  exit 0
fi

echo >&2
echo "# applying..." >&2
# Hand az a path az can actually open.
#
# `az` on Windows is a native binary, and $STRIPPED is an MSYS path like
# /tmp/redis-apply-ncKHLv.yaml. Git Bash normally rewrites that into a
# Windows path on the way through -- but MSYS_NO_PATHCONV=1 disables the
# rewrite, and its sibling create-alerts.sh REQUIRES that variable, because
# every argument there is a /subscriptions/... resource ID that the same
# rewrite would corrupt. Two scripts in one directory with opposite needs
# is a trap; anyone who sets the variable once, or exports it, gets
# "does not exist" here.
#
# Converting explicitly makes this script correct either way: with the
# variable, without it, and on Linux, where there is no cygpath and the
# POSIX path was already right.
YAML_ARG="$STRIPPED"
if command -v cygpath >/dev/null 2>&1; then
  YAML_ARG="$(cygpath -w "$STRIPPED")"
fi

if ! az containerapp update -g "$RG" -n "$APP" --yaml "$YAML_ARG" --output none; then
  echo "FAILED: az containerapp update returned non-zero." >&2
  exit 1
fi

# --- verify, rather than assume --------------------------------------
FAILURES=0

secret_names=$(az containerapp secret list -g "$RG" -n "$APP" --query "[].name" -o tsv 2>/dev/null)
if ! printf '%s' "$secret_names" | grep -qx "redis-password"; then
  echo "FAILED: the redis-password secret is gone after the update." >&2
  FAILURES=$((FAILURES + 1))
fi

live_args=$(az containerapp show -g "$RG" -n "$APP" \
  --query "properties.template.containers[0].args[0]" -o tsv 2>/dev/null)
for expected in "--maxmemory 384mb" "--maxmemory-policy volatile-lru" "--appendonly no"; do
  if ! printf '%s' "$live_args" | grep -qF -- "$expected"; then
    echo "FAILED: live args do not contain '${expected}'." >&2
    FAILURES=$((FAILURES + 1))
  fi
done
if printf '%s' "$live_args" | grep -qF -- "REPLACE_AT_DEPLOY_TIME"; then
  echo "FAILED: the placeholder reached the live container." >&2
  FAILURES=$((FAILURES + 1))
fi

if [ "$FAILURES" -eq 0 ]; then
  echo "redis-cc updated; redis-password intact; args verified." >&2
  echo >&2
  echo "Confirm clients reconnected -- the update rolls a new revision, so" >&2
  echo "every Redis connection is dropped and the cache and session stores" >&2
  echo "start empty. Queued Horizon jobs in flight at that moment are lost;" >&2
  echo "there is no volume (see redis.yaml's header for why)." >&2
  exit 0
fi
echo "redis-cc update completed with ${FAILURES} verification failure(s)." >&2
exit 1
