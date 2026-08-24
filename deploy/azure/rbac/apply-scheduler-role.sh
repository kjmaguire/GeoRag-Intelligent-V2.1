#!/usr/bin/env bash
# Put the two nightly scheduler jobs on a least-privilege role.
#
#   bash deploy/azure/rbac/apply-scheduler-role.sh            # show
#   bash deploy/azure/rbac/apply-scheduler-role.sh --apply    # do it
#
# shutdown-scheduler-cc and startup-scheduler-cc held **Contributor over
# the whole resource group** to make two write calls between them:
#
#   az containerapp update --min-replicas ...   -> Microsoft.App/containerApps/write
#   az postgres flexible-server stop|start      -> .../flexibleServers/{stop,start}/action
#
# Contributor also lets them delete the database, rotate the storage
# account, and reconfigure every app in the group. Two cron jobs whose
# entire purpose is to save money overnight do not need that.
#
# The custom role is in georag-nightly-scheduler-role.json. It grants
# `*/read` deliberately: reads are not the risk (secret retrieval is a
# POST action, not a `/read`, so listKeys / listSecrets are still denied),
# and enumerating every read action these two scripts might make is how
# you end up with a nightly job that fails silently on a `show` you forgot.
#
# TESTING NOTE. You cannot verify this by running the jobs by hand -- both
# sweeps open with a DST double-fire guard that exits 0 unless the
# US-Pacific local hour matches their target, so a manual start is a no-op.
# The real test is the next scheduled fire. If a sweep starts failing on
# authorization, re-grant Contributor to unblock the night and add the
# missing action to the role definition rather than leaving it broad:
#
#   az role assignment create --assignee-object-id <pid> \
#     --assignee-principal-type ServicePrincipal --role Contributor \
#     --scope /subscriptions/<sub>/resourceGroups/georag
set -uo pipefail

RG=georag
SUBSCRIPTION=d314ab40-b5b7-4e3e-8308-86023fb7638a
ROLE="GeoRAG Nightly Scheduler"
SCOPE="/subscriptions/${SUBSCRIPTION}/resourceGroups/${RG}"
JOBS="shutdown-scheduler-cc startup-scheduler-cc"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${HERE}/../_host_compat.sh"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

# MSYS_NO_PATHCONV: every argument below is an ARM resource id, and Git
# Bash rewrites a leading / into a Windows drive path. See create-alerts.sh.
export MSYS_NO_PATHCONV=1

if ! az role definition list --name "$ROLE" --scope "$SCOPE" --query "[0].roleName" -o tsv 2>/dev/null | strip_cr | grep -q .; then
    echo "role '${ROLE}' does not exist yet"
    if [ "$APPLY" -eq 1 ]; then
        az role definition create \
            --role-definition "$(to_host_path "${HERE}/georag-nightly-scheduler-role.json")" \
            -o none && echo "  created"
    fi
else
    echo "role '${ROLE}' exists"
fi

for job in $JOBS; do
    pid="$(az containerapp job show -g "$RG" -n "$job" --query identity.principalId -o tsv 2>/dev/null | strip_cr)"
    if [ -z "$pid" ] || [ "$pid" = "None" ]; then
        echo "== ${job}: no system-assigned identity -- skipped"
        continue
    fi

    has_role="$(az role assignment list --subscription "$SUBSCRIPTION" --resource-group "$RG" \
        --query "[?principalId=='${pid}'].roleDefinitionName" -o tsv 2>/dev/null | strip_cr | tr '\n' ' ')"
    echo "== ${job} (${pid}): ${has_role:-none}"

    [ "$APPLY" -eq 0 ] && continue

    case "$has_role" in
        *"$ROLE"*) : ;;
        *) az role assignment create --assignee-object-id "$pid" \
               --assignee-principal-type ServicePrincipal \
               --role "$ROLE" --scope "$SCOPE" -o none && echo "   granted ${ROLE}" ;;
    esac

    # Only after the narrow role is in place, and only then.
    case "$has_role" in
        *Contributor*) az role assignment delete --assignee "$pid" --role Contributor \
                           --scope "$SCOPE" -o none 2>/dev/null && echo "   revoked Contributor" ;;
    esac
done

echo
echo "-- assignments on ${RG} --"
az role assignment list --subscription "$SUBSCRIPTION" --resource-group "$RG" \
    --query "[].{role:roleDefinitionName,principal:principalId}" -o tsv 2>/dev/null | strip_cr

[ "$APPLY" -eq 0 ] && echo && echo "dry run -- nothing changed. Re-run with --apply."
exit 0
