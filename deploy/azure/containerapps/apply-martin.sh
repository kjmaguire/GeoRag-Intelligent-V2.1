#!/usr/bin/env bash
# Apply deploy/azure/containerapps/martin.yaml to martin-cc, safely.
#
#   bash deploy/azure/containerapps/apply-martin.sh            # show
#   bash deploy/azure/containerapps/apply-martin.sh --apply    # do it
#
# Runs from Git Bash, WSL or native bash as-is. Mirrors apply-redis.sh —
# read that script's header for the full reasoning behind the secrets strip.
#
# ---------------------------------------------------------------------
# THE SECRET IS NOT IN GIT, AND THIS SCRIPT DOES NOT ASK FOR IT
# ---------------------------------------------------------------------
# martin.yaml carries the shape Container Apps requires, which includes a
# `secrets:` block whose value is the placeholder REPLACE_AT_DEPLOY_TIME.
# Sending it verbatim sets the live `martin-database-url` secret to that
# literal string, and Martin then fails to start against a database that is
# perfectly healthy.
#
# So the copy this script sends has the secrets block removed. Omitting the
# block leaves the existing secret untouched; providing it replaces it. A
# `secretRef:` further down the template is a REFERENCE and must survive —
# stripping that would be a different bug with the same symptom, so it is
# checked for separately.
#
# SET THE SECRET YOURSELF, once, before the first --apply:
#
#   az containerapp secret set -g georag -n martin-cc \
#     --secrets martin-database-url="postgresql://martin_readonly:PASSWORD@georag-pg-cc.postgres.database.azure.com:5432/georag?sslmode=require"
#
# Use the `martin_readonly` role, not georag_app. It holds EXECUTE on the
# tile functions and nothing else, so a bug in a tile function's argument
# handling cannot become a data-exfiltration path.
#
# Connect to Postgres DIRECTLY, not through PgBouncer: Martin keeps a pool of
# 20 and issues prepared statements, which transaction pooling breaks.
# ---------------------------------------------------------------------
set -uo pipefail

RG=georag
APP=martin-cc
ENVIRONMENT=georag-env-cc
SUBSCRIPTION=d314ab40-b5b7-4e3e-8308-86023fb7638a
ACR_SERVER=georagacrcc.azurecr.io
ACR_SCOPE="/subscriptions/${SUBSCRIPTION}/resourceGroups/${RG}/providers/Microsoft.ContainerRegistry/registries/georagacrcc"

#: Upstream Martin, used ONLY to mint the app's identity on first create —
#: see the three-step comment below. It carries no martin.yaml, so a container
#: left on this image will not serve tiles; step 3 replaces it immediately.
PUBLIC_IMAGE="ghcr.io/maplibre/martin:1.11.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${HERE}/martin.yaml"

. "${HERE}/../_host_compat.sh"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

STRIPPED="$(mktemp -t martin-apply-XXXXXX.yaml)"
trap 'rm -f "$STRIPPED"' EXIT

# Drop the `secrets:` mapping and everything nested under it, by indentation.
# Nothing else in the file is touched.
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

# Comments are not payload — martin.yaml's own header explains the
# placeholder at length, and grepping the whole file would match that prose
# and abort on every run. apply-redis.sh had exactly that bug and never got
# past this point.
payload="$(grep -v '^[[:space:]]*#' "$STRIPPED")"

if printf '%s\n' "$payload" | grep -q "REPLACE_AT_DEPLOY_TIME"; then
  echo "ABORT: the placeholder survived the strip -- refusing to send it." >&2
  exit 1
fi

if printf '%s\n' "$payload" | grep -qE '^[[:space:]]*secrets:[[:space:]]*$'; then
  echo "ABORT: the secrets mapping is still present -- refusing to send it." >&2
  exit 1
fi

# The secretRef MUST survive: without it DATABASE_URL is unset and Martin
# boots with no database at all.
if ! printf '%s\n' "$payload" | grep -q "secretRef: martin-database-url"; then
  echo "ABORT: the strip removed the secretRef -- Martin would start with no database." >&2
  exit 1
fi

EXISTS=0
az containerapp show -g "$RG" -n "$APP" >/dev/null 2>&1 && EXISTS=1

echo "# --- what is live now ---" >&2
if [ "$EXISTS" -eq 1 ]; then
  az containerapp show -g "$RG" -n "$APP" \
    --query "{image:properties.template.containers[0].image,cpu:properties.template.containers[0].resources.cpu,memory:properties.template.containers[0].resources.memory,external:properties.configuration.ingress.external}" \
    -o yaml >&2 2>/dev/null
else
  echo "martin-cc does not exist yet — this run will CREATE it." >&2
fi

echo >&2
echo "# --- what this would set ---" >&2
# `- image:` is a list item, so it needs the optional dash — without it the
# one line an operator most wants to see is the one that does not print.
grep -E "^\s+-?\s*(image:|cpu:|memory:|external:|targetPort:|minReplicas:|maxReplicas:)" "$STRIPPED" >&2

if [ "$APPLY" -eq 0 ]; then
  echo >&2
  echo "# dry run. Re-run with --apply." >&2
  if [ "$EXISTS" -eq 1 ]; then
    echo "#   az containerapp update -g $RG -n $APP --yaml <secrets-stripped copy>" >&2
  else
    echo "#   az containerapp create -g $RG -n $APP --environment $ENVIRONMENT --yaml <copy>" >&2
    echo "#" >&2
    echo "# NOTE: on FIRST create the secret must exist or Martin cannot start." >&2
    echo "# Set it yourself (this script never handles the password):" >&2
    echo "#   az containerapp secret set -g $RG -n $APP --secrets martin-database-url=\"postgresql://martin_readonly:...@georag-pg-cc.postgres.database.azure.com:5432/georag?sslmode=require\"" >&2
  fi
  exit 0
fi

echo >&2
echo "# applying..." >&2
YAML_ARG="$(to_host_path "$STRIPPED")"

if [ "$EXISTS" -eq 1 ]; then
  if ! az containerapp update -g "$RG" -n "$APP" --yaml "$YAML_ARG" --output none; then
    echo "FAILED: az containerapp update returned non-zero." >&2
    exit 1
  fi
else
  # CREATE IS THREE STEPS, NOT ONE, and the order is forced by a
  # chicken-and-egg: the app pulls from a private ACR using its own
  # system-assigned identity, and that identity does not exist until the app
  # does. Creating straight from the ACR image fails with
  #   UNAUTHORIZED: authentication required
  # and leaves the app in ProvisioningState=Failed, which cannot then be
  # modified — `identity assign` answers ResourceNotProvisioned and the only
  # way out is to delete it. Measured on 2026-08-25.
  #
  # So: create from the PUBLIC upstream image to mint the identity, grant it
  # AcrPull, then switch to our image.
  #
  # The secrets block cannot be omitted on create — there is no existing
  # secret to leave alone, and a secretRef to a secret that does not exist is
  # rejected. It gets the placeholder; rotate-martin-credential.sh sets the
  # real value. The app stays unhealthy in between, which is the honest state.
  echo "# 1/3 creating from the public upstream image to mint an identity..." >&2
  if ! az containerapp create -g "$RG" -n "$APP" \
      --environment "$ENVIRONMENT" \
      --image "$PUBLIC_IMAGE" \
      --system-assigned \
      --min-replicas 0 --max-replicas 1 \
      --secrets "martin-database-url=REPLACE_AT_DEPLOY_TIME" \
      --output none; then
    echo "FAILED: az containerapp create returned non-zero." >&2
    exit 1
  fi

  PRINCIPAL=$(az containerapp show -g "$RG" -n "$APP" \
    --query "identity.principalId" -o tsv 2>/dev/null | strip_cr)
  if [ -z "$PRINCIPAL" ]; then
    echo "FAILED: the app was created but has no system-assigned principal." >&2
    exit 1
  fi

  echo "# 2/3 granting AcrPull to ${PRINCIPAL}..." >&2
  if ! az role assignment create \
      --assignee-object-id "$PRINCIPAL" \
      --assignee-principal-type ServicePrincipal \
      --role AcrPull \
      --scope "$ACR_SCOPE" \
      --output none 2>/dev/null; then
    # This is the step most likely to be refused, and the failure mode is
    # confusing if it is not called out: creating a role assignment needs
    # User Access Administrator or Owner, which a deploy service principal
    # usually does NOT have. Everything up to here has succeeded, so the app
    # exists and only needs the grant.
    cat >&2 <<GRANT
FAILED: could not grant AcrPull — this identity cannot create role assignments.

martin-cc EXISTS and has principal ${PRINCIPAL}. It just cannot pull from
the registry yet. Someone with User Access Administrator or Owner on the
subscription needs to run:

  az role assignment create \\
    --assignee-object-id ${PRINCIPAL} \\
    --assignee-principal-type ServicePrincipal \\
    --role AcrPull \\
    --scope ${ACR_SCOPE}

Then re-run this script with --apply to switch to the real image.
GRANT
    exit 1
  fi

  echo "# 3/3 switching to the ACR image..." >&2
  if ! az containerapp registry set -g "$RG" -n "$APP" \
      --server "$ACR_SERVER" --identity system --output none; then
    echo "FAILED: could not point the app's registry at the system identity." >&2
    exit 1
  fi
  # Now apply the real shape over the bare app.
  if ! az containerapp update -g "$RG" -n "$APP" --yaml "$YAML_ARG" --output none; then
    echo "FAILED: created martin-cc but the manifest update returned non-zero." >&2
    exit 1
  fi
fi

# --- verify, rather than assume --------------------------------------
FAILURES=0

secret_names=$(az containerapp secret list -g "$RG" -n "$APP" --query "[].name" -o tsv 2>/dev/null | strip_cr)
if ! printf '%s' "$secret_names" | grep -qx "martin-database-url"; then
  echo "FAILED: the martin-database-url secret is gone after the update." >&2
  FAILURES=$((FAILURES + 1))
fi

external=$(az containerapp show -g "$RG" -n "$APP" \
  --query "properties.configuration.ingress.external" -o tsv 2>/dev/null | strip_cr)
if [ "$external" != "false" ]; then
  # This is the security-relevant one. Laravel's proxy runs the
  # project-access check; a browser reaching Martin directly bypasses it, and
  # every silver tile is workspace-scoped data.
  echo "FAILED: ingress.external is '${external}', expected false." >&2
  FAILURES=$((FAILURES + 1))
fi

if [ "$FAILURES" -eq 0 ]; then
  echo "martin-cc applied; secret present; ingress is internal-only." >&2
  echo >&2
  echo "If this was the first create, the secret still holds the placeholder." >&2
  echo "Martin will not start until you set the real connection string:" >&2
  echo "  az containerapp secret set -g $RG -n $APP --secrets martin-database-url=\"postgresql://martin_readonly:...@georag-pg-cc.postgres.database.azure.com:5432/georag?sslmode=require\"" >&2
  echo >&2
  echo "Then confirm: curl -s -o /dev/null -w '%{http_code}' <octane>/tiles/silver/pg_collars_by_project/0/0/0.pbf?project_id=<uuid>" >&2
  exit 0
fi
echo "martin-cc apply completed with ${FAILURES} verification failure(s)." >&2
exit 1
