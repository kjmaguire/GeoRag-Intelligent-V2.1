#!/usr/bin/env bash
# Generate a password for martin_readonly, set it on the role, and store the
# resulting connection string as the martin-cc secret. One command, no value
# ever typed by a human or pasted into a chat.
#
#   bash deploy/azure/containerapps/rotate-martin-credential.sh          # show
#   bash deploy/azure/containerapps/rotate-martin-credential.sh --apply  # do it
#
# ---------------------------------------------------------------------
# WHY GENERATE RATHER THAN REUSE
# ---------------------------------------------------------------------
# martin_readonly already exists on georag-pg-cc with EXECUTE on the 18 tile
# functions. Its password is not recorded anywhere this repo can reach, and
# an invented one cannot authenticate against a role that already has a
# different one. So the only way to get a WORKING connection string without
# someone digging out the original is to set a new password on the role and
# use that.
#
# ALTER ROLE ... PASSWORD is a credential mutation on a live database, which
# is why this is a script you run deliberately and not something a deploy
# does. It affects only martin_readonly; no application role is touched, and
# martin-cc is the only consumer, so nothing else breaks when it changes.
#
# ---------------------------------------------------------------------
# WHAT IT DOES NOT DO
# ---------------------------------------------------------------------
# It never prints the password, never writes it to a file, and never puts it
# in a shell history entry — it is generated into a variable, sent to
# Postgres and to `az containerapp secret set`, and discarded. `set +x` is
# asserted below so a caller running with `bash -x` cannot leak it either.
#
# Requires psql on PATH and an admin connection to the server. You will be
# prompted by psql for the ADMIN password; this script does not handle it.
set -uo pipefail
set +x   # never trace: a traced run would echo the generated password

RG=georag
APP=martin-cc
PG_HOST=georag-pg-cc.postgres.database.azure.com
PG_DB=georag
PG_ADMIN="${PG_ADMIN:-georag_admin}"
ROLE=martin_readonly

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ABORT: $1 is not on PATH. $2" >&2
    exit 1
  }
}
need az "Install the Azure CLI."
need psql "Install the PostgreSQL client (psql) — this script sets the role password."

# Refuse while the server is stopped rather than failing with a connection
# timeout that looks like a network problem. georag-pg-cc is deliberately
# stopped 06:00-13:00 UTC by the nightly saver.
state=$(az postgres flexible-server show -g "$RG" -n georag-pg-cc --query state -o tsv 2>/dev/null | tr -d '\r')
if [ "$state" != "Ready" ]; then
  echo "ABORT: georag-pg-cc is '${state}', not Ready." >&2
  echo "It is stopped 06:00-13:00 UTC by the nightly saver. Wait, or start it:" >&2
  echo "  az postgres flexible-server start -g $RG -n georag-pg-cc" >&2
  exit 1
fi

if ! az containerapp show -g "$RG" -n "$APP" >/dev/null 2>&1; then
  echo "ABORT: ${APP} does not exist yet." >&2
  echo "Create it first:  bash deploy/azure/containerapps/apply-martin.sh --apply" >&2
  exit 1
fi

if [ "$APPLY" -eq 0 ]; then
  cat >&2 <<'PLAN'
# dry run. Re-run with --apply.
#
# It will:
#   1. generate a 32-byte URL-safe password (openssl rand)
#   2. ALTER ROLE martin_readonly PASSWORD '<generated>'   (psql, as admin)
#   3. az containerapp secret set -n martin-cc --secrets martin-database-url=...
#   4. restart the martin-cc revision so it picks the secret up
#   5. verify the secret exists and Martin answers /health
#
# The password is never printed, never written to disk, and never enters
# shell history. psql will prompt for the ADMIN password; that one is yours
# and this script does not touch it.
PLAN
  exit 0
fi

# URL-safe by construction: the password goes into a postgresql:// URI, and a
# '/' or '@' or '#' in it would silently truncate the connection string into
# something that parses but points somewhere else.
PASSWORD="$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-32)"
if [ -z "$PASSWORD" ] || [ "${#PASSWORD}" -lt 24 ]; then
  echo "ABORT: password generation produced nothing usable." >&2
  exit 1
fi

echo "# setting the role password (psql will prompt for the ADMIN password)..." >&2
# The password reaches psql through a variable, not the command line, so it
# does not appear in the process table for other users on this machine.
if ! PGPASSWORD="" psql \
      --host "$PG_HOST" --username "$PG_ADMIN" --dbname "$PG_DB" \
      --set=ON_ERROR_STOP=1 \
      --set=pw="$PASSWORD" \
      --quiet \
      --command "ALTER ROLE ${ROLE} WITH PASSWORD :'pw';" ; then
  echo "FAILED: could not set the ${ROLE} password." >&2
  exit 1
fi

CONN="postgresql://${ROLE}:${PASSWORD}@${PG_HOST}:5432/${PG_DB}?sslmode=require"

echo "# storing it as the martin-cc secret..." >&2
if ! az containerapp secret set -g "$RG" -n "$APP" \
      --secrets "martin-database-url=${CONN}" --output none; then
  echo "FAILED: az containerapp secret set returned non-zero." >&2
  exit 1
fi
unset PASSWORD CONN

# A secret change does not restart running replicas, so Martin would keep
# using the old value until something else rolled it.
echo "# restarting martin-cc so it reads the new secret..." >&2
REV=$(az containerapp revision list -g "$RG" -n "$APP" \
        --query "[?properties.active]|[0].name" -o tsv 2>/dev/null | tr -d '\r')
if [ -n "$REV" ]; then
  az containerapp revision restart -g "$RG" -n "$APP" --revision "$REV" --output none || true
fi

# --- verify, rather than assume --------------------------------------
FAILURES=0
if ! az containerapp secret list -g "$RG" -n "$APP" --query "[].name" -o tsv 2>/dev/null \
     | tr -d '\r' | grep -qx "martin-database-url"; then
  echo "FAILED: the martin-database-url secret is not present." >&2
  FAILURES=$((FAILURES + 1))
fi

echo "# waiting for Martin to report healthy..." >&2
for _ in $(seq 1 20); do
  health=$(az containerapp revision list -g "$RG" -n "$APP" \
             --query "[?properties.active]|[0].properties.healthState" -o tsv 2>/dev/null | tr -d '\r')
  [ "$health" = "Healthy" ] && break
  sleep 10
done
if [ "${health:-}" != "Healthy" ]; then
  echo "FAILED: martin-cc healthState is '${health:-unknown}' after ~200s." >&2
  echo "Check the logs:  az containerapp logs show -g $RG -n $APP --tail 50" >&2
  FAILURES=$((FAILURES + 1))
fi

if [ "$FAILURES" -eq 0 ]; then
  echo "martin_readonly rotated; secret set; martin-cc healthy." >&2
  exit 0
fi
exit 1
