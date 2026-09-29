#!/usr/bin/env bash
# Render a Terraform plan as the Markdown run summary for
# .github/workflows/terraform.yml.
#
#   bash deploy/aws/ci/summarise-plan.sh changes.json plan.txt
#
# changes.json is the workflow's change list: a sorted array of
# {address, actions}. plan.txt is `terraform show -no-color` output.
#
# The part that matters is the WARNING block. Most of this stack can be
# replaced without anyone noticing; a handful of resources hold the only
# copy of something (the database, Qdrant's EFS volume, the object store,
# the app secret, the image registry) or take retrieval down while they
# are recreated. A replace or delete on one of those is called out above
# everything else, so an approval is never given with it buried on line
# 300 of the plan text.
set -euo pipefail

CHANGES="${1:?usage: summarise-plan.sh changes.json plan.txt}"
PLAN_TEXT="${2:?usage: summarise-plan.sh changes.json plan.txt}"

# Resource types whose delete or replace loses data or takes a store down,
# plus the one service whose replace means a retrieval outage.
STATEFUL='^(aws_db_instance|aws_efs_file_system|aws_efs_access_point|aws_efs_mount_target|aws_s3_bucket|aws_secretsmanager_secret|aws_ecr_repository|aws_iam_openid_connect_provider)\.|^aws_ecs_service\.this\["qdrant"\]$'

count() { jq --arg want "$1" '[.[] | select((.actions | join(",")) == $want)] | length' "$CHANGES"; }

creates=$(count "create")
updates=$(count "update")
deletes=$(count "delete")
replaces=$(jq '[.[] | select((.actions | index("delete")) and (.actions | index("create")))] | length' "$CHANGES")

echo "## Terraform plan"
echo
if [ "$(jq 'length' "$CHANGES")" -eq 0 ]; then
  echo "No changes. The infrastructure matches the configuration."
  exit 0
fi
echo "**${creates} to create, ${updates} to update, ${replaces} to replace, ${deletes} to destroy.**"
echo

danger=$(jq -r --arg re "$STATEFUL" '
  .[] | select((.actions | index("delete")) and (.address | test($re)))
  | "- `\(.address)`: \(.actions | join(" then "))"' "$CHANGES")
if [ -n "$danger" ]; then
  echo "> [!WARNING]"
  echo "> **Destroys or replaces a stateful resource.** Read the plan below for each"
  echo "> line before approving. A replace of the database, an EFS volume or the"
  echo "> object store loses its data; a Qdrant service replace is a retrieval outage."
  echo
  echo "$danger"
  echo
fi

echo "| Resource | Action |"
echo "|---|---|"
jq -r '.[] | "| `\(.address)` | \(.actions | join(" then ")) |"' "$CHANGES"
echo
echo "<details><summary>Full plan</summary>"
echo
echo '```'
# The step summary is capped at 1 MiB; keep well under it.
head -c 400000 "$PLAN_TEXT"
echo
echo '```'
echo "</details>"
