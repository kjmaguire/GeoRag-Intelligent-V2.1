#!/usr/bin/env bash
# Seed the two-tenant corpus into the LIVE database and verify the Martin
# tenant fence against it.
#
# Runs as a one-off ECS task on the migrate task definition, which is the
# only image in this deployment that already holds database credentials and
# sits inside the VPC. Same shape as the Qdrant bootstrap in
# deploy/aws/README.md Step 4 — nothing here grants a standing capability.
#
# This is a REHEARSAL tool. It writes two workspaces, two projects and six
# collars, all slug-prefixed `rehearsal-`, and ops/rehearsal/teardown.sql
# removes exactly those rows. Do not point it at a database carrying real
# tenant data without reading that teardown first.
#
# Usage:
#   bash ops/rehearsal/run_against_deployment.sh seed
#   bash ops/rehearsal/run_against_deployment.sh verify
set -euo pipefail

CLUSTER="${ECS_CLUSTER:-georag}"
ACTION="${1:-verify}"

case "$ACTION" in
  seed)     SQL_FILE=seed_multitenant_corpus.sql ;;
  verify)   SQL_FILE=verify_tenant_fence.sql ;;
  teardown) SQL_FILE=teardown_multitenant_corpus.sql ;;
  *) echo "usage: $0 {seed|verify|teardown}" >&2; exit 2 ;;
esac

# Subnets/SG from a running service rather than terraform: the 2026-09-18
# rehearsal found no operator terraform environment existed anywhere, and
# this needs none.
NET=$(aws ecs describe-services --cluster "$CLUSTER" --services fastapi \
        --query 'services[0].networkConfiguration.awsvpcConfiguration')
SUBNETS=$(echo "$NET" | jq -r '.subnets|join(",")')
SG=$(echo "$NET" | jq -r '.securityGroups|join(",")')

echo "running ops/rehearsal/${SQL_FILE} against ${CLUSTER}..."

# The SQL travels in the command override rather than being baked into an
# image: these files change during a rehearsal and rebuilding the migrate
# image for each edit would make the loop useless.
SQL_TEXT=$(cat "$(dirname "$0")/${SQL_FILE}")

TASK_ARN=$(aws ecs run-task --cluster "$CLUSTER" \
  --task-definition georag-migrate \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides "$(jq -n --arg sql "$SQL_TEXT" '{
      containerOverrides: [{
        name: "migrate",
        command: ["sh","-lc","printf %s \"$GEORAG_REHEARSAL_SQL\" | psql \"$DATABASE_URL\" -v ON_ERROR_STOP=1"],
        environment: [{name:"GEORAG_REHEARSAL_SQL", value:$sql}]
      }]}')" \
  --query 'tasks[0].taskArn' --output text)

echo "task: $TASK_ARN"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)

echo "── output ───────────────────────────────────────────────"
aws logs get-log-events --log-group-name /ecs/georag \
  --log-stream-name "migrate/migrate/${TASK_ARN##*/}" \
  --query 'events[].message' --output text 2>/dev/null | tr '\t' '\n' \
  || echo "(could not read the task log)"
echo "─────────────────────────────────────────────────────────"

# psql runs with ON_ERROR_STOP, and every check in verify_tenant_fence.sql
# RAISEs, so the container's exit code IS the verdict. A silent pass is not
# reachable: section 1 fails the run rather than letting the cross-tenant
# assertions go vacuous.
echo "exit code: ${EXIT_CODE}"
[ "$EXIT_CODE" = "0" ] || { echo "::error::${SQL_FILE} FAILED"; exit 1; }
echo "${SQL_FILE} OK"
