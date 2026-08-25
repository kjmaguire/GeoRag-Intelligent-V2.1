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
# psql is NOT checked here. It is needed only by --apply, and checking it up
# front meant a dry run could not even print its plan on a machine without
# the client installed — which is exactly the machine an operator is reading
# the plan on before deciding to install anything. Checked below, immediately
# after the dry-run branch exits.

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

# ---------------------------------------------------------------------
# BOOTSTRAP: the app may not exist yet, and that is not an error
# ---------------------------------------------------------------------
# This used to ABORT and tell the operator to run apply-martin.sh first.
# Since 2026-08-25 apply-martin.sh refuses to create without a credential
# already in hand (creating with a placeholder makes an app that crashloops
# and can then never be updated), and it points back here to mint one. Two
# scripts each telling the operator to run the other is a deadlock, and it
# is the state martin-cc was left in after being deleted.
#
# The two halves are separable: minting the password needs Postgres, NOT the
# container app. Only STORING it needs the app. So when the app is absent
# this script mints the credential and hands it to apply-martin.sh through
# the environment of a child process — the value never lands on a command
# line, in a file, or on the terminal, which is the invariant this whole
# script exists to preserve.
BOOTSTRAP=0
if ! az containerapp show -g "$RG" -n "$APP" >/dev/null 2>&1; then
  BOOTSTRAP=1
fi

APPLY_MARTIN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/apply-martin.sh"
if [ "$BOOTSTRAP" -eq 1 ] && [ ! -f "$APPLY_MARTIN" ]; then
  echo "ABORT: ${APP} does not exist and apply-martin.sh was not found at" >&2
  echo "  ${APPLY_MARTIN}" >&2
  echo "Cannot bootstrap without it." >&2
  exit 1
fi

if [ "$APPLY" -eq 0 ]; then
  if [ "$BOOTSTRAP" -eq 1 ]; then
    cat >&2 <<'PLAN'
# dry run — BOOTSTRAP mode: martin-cc does not exist.
#
# It will:
#   1. generate a 32-byte URL-safe password (openssl rand)
#   2. ALTER ROLE martin_readonly PASSWORD '<generated>'   (psql, as admin)
#   3. run apply-martin.sh --apply with MARTIN_DATABASE_URL set in the child
#      process's environment, which CREATES martin-cc with a working
#      credential from the very first revision
#   4. verify the secret exists and Martin answers /health
#
# Step 3 is why the app is created with a real credential rather than a
# placeholder: an app whose first revision cannot start locks itself in
# provisioningState=InProgress and then refuses every update, including the
# secret set. Delete is the only exit. Measured 2026-08-25.
PLAN
  else
    cat >&2 <<'PLAN'
# dry run. Re-run with --apply.
#
# It will:
#   1. generate a 32-byte URL-safe password (openssl rand)
#   2. ALTER ROLE martin_readonly PASSWORD '<generated>'   (psql, as admin)
#   3. az containerapp secret set -n martin-cc --secrets martin-database-url=...
#   4. restart the martin-cc revision so it picks the secret up
#   5. verify the secret exists and Martin answers /health
PLAN
  fi
  cat >&2 <<'PLAN'
#
# The password is never printed, never written to disk, and never enters
# shell history. psql will prompt for the ADMIN password; that one is yours
# and this script does not touch it.
PLAN
  if ! command -v psql >/dev/null 2>&1; then
    echo "#" >&2
    echo "# NOTE: psql is not on PATH. --apply will need it; the plan above" >&2
    echo "#       is accurate either way." >&2
  fi
  exit 0
fi

need psql "Install the PostgreSQL client (psql) — this script sets the role password."

# URL-safe by construction: the password goes into a postgresql:// URI, and a
# '/' or '@' or '#' in it would silently truncate the connection string into
# something that parses but points somewhere else.
PASSWORD="$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-32)"
if [ -z "$PASSWORD" ] || [ "${#PASSWORD}" -lt 24 ]; then
  echo "ABORT: password generation produced nothing usable." >&2
  exit 1
fi

# Belt and braces before the password is interpolated into SQL below. The
# generator above yields base64 with '=+/' stripped, so it is strictly
# alphanumeric and cannot carry a quote or a backslash — but relying on that
# from a distance is how an injection gets introduced by a later "improvement"
# to the generator. Assert it here, next to the use.
case "$PASSWORD" in
  *[!A-Za-z0-9]*)
    echo "ABORT: generated password is not strictly alphanumeric; refusing" >&2
    echo "to interpolate it into SQL. Fix the generator above." >&2
    exit 1
    ;;
esac

echo "# setting the role password (psql will prompt for the ADMIN password)..." >&2
# ON STDIN, not --command and not --set, for two separate reasons.
#
# 1. CORRECTNESS. `psql --command` hands the string to the server verbatim;
#    psql only expands `:'var'` in the lexer it uses for stdin and -f input.
#    So `--set=pw=... --command "... PASSWORD :'pw';"` sent a literal colon and
#    the server answered `syntax error at or near ":"`. Measured 2026-08-25 in
#    Azure Cloud Shell.
#
# 2. SECRECY. `--set=pw="$PASSWORD"` is a COMMAND-LINE ARGUMENT — the password
#    was in argv and visible to `ps` for every other user on the host, which is
#    the exact opposite of what the comment here used to claim. stdin is
#    neither argv nor the environment, so a pipe leaks nothing to the process
#    table.
#
# psql still prompts for the ADMIN password on /dev/tty, not stdin, so feeding
# SQL through the pipe does not interfere with it.
# WITH LOGIN, not just PASSWORD.
#
# The role was created `CREATE ROLE martin_readonly NOLOGIN ...` by
# 2026_04_22_130000_create_silver_mvt_functions.php, whose comment says a
# later chunk would finish configuring it. That never happened, because
# Martin was never actually deployed until 2026-08-25 — so the role has held
# EXECUTE on all 18 tile functions and been unable to open a session the
# whole time. Martin got as far as authenticating and then:
#
#     FATAL: role "martin_readonly" is not permitted to log in   (SQLSTATE 28000)
#
# Setting a password on a NOLOGIN role is provisioning half a credential, and
# this script exists to provision a WORKING one. Idempotent: re-running on a
# role that already has LOGIN is a no-op for that attribute.
if ! printf "ALTER ROLE %s WITH LOGIN PASSWORD '%s';\n" "$ROLE" "$PASSWORD" \
    | PGPASSWORD="" psql \
      --host "$PG_HOST" --username "$PG_ADMIN" --dbname "$PG_DB" \
      --set=ON_ERROR_STOP=1 \
      --quiet \
      --file - ; then
  echo "FAILED: could not set the ${ROLE} password." >&2
  echo "If the role does not exist, creating it here would be worse than" >&2
  echo "failing: Martin would connect and then 403 on every tile, because" >&2
  echo "the EXECUTE grants on the tile functions come from init-roles.sql." >&2
  exit 1
fi

CONN="postgresql://${ROLE}:${PASSWORD}@${PG_HOST}:5432/${PG_DB}?sslmode=require"

if [ "$BOOTSTRAP" -eq 1 ]; then
  # Create the app WITH the credential. Passed through the child's
  # environment rather than an argument so it never reaches the process
  # table, and scoped to this one invocation via `env` rather than exported
  # into the rest of this shell.
  echo "# martin-cc does not exist — creating it with this credential..." >&2
  if ! MARTIN_DATABASE_URL="$CONN" bash "$APPLY_MARTIN" --apply; then
    unset PASSWORD CONN
    echo "FAILED: apply-martin.sh could not create ${APP}." >&2
    echo "The ${ROLE} password WAS rotated, so re-running this script is safe" >&2
    echo "and will mint a fresh one." >&2
    exit 1
  fi
else
  echo "# storing it as the martin-cc secret..." >&2
  if ! az containerapp secret set -g "$RG" -n "$APP" \
        --secrets "martin-database-url=${CONN}" --output none; then
    unset PASSWORD CONN
    echo "FAILED: az containerapp secret set returned non-zero." >&2
    exit 1
  fi
fi
unset PASSWORD CONN

# A secret change does not restart running replicas, so Martin would keep
# using the old value until something else rolled it. Not needed after a
# bootstrap: the app was just created and its first revision already booted
# with this credential.
if [ "$BOOTSTRAP" -eq 0 ]; then
  echo "# restarting martin-cc so it reads the new secret..." >&2
  REV=$(az containerapp revision list -g "$RG" -n "$APP" \
          --query "[?properties.active]|[0].name" -o tsv 2>/dev/null | tr -d '\r')
  if [ -n "$REV" ]; then
    az containerapp revision restart -g "$RG" -n "$APP" --revision "$REV" --output none || true
  fi
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
