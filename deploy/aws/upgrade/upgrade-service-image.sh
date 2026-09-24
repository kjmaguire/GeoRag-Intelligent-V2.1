#!/usr/bin/env bash
# Move one running ECS service onto a new image, changing nothing else.
#
#   bash upgrade-service-image.sh SERVICE [IMAGE]            # show what would change
#   bash upgrade-service-image.sh SERVICE [IMAGE] --apply    # do it
#
# IMAGE defaults to the service's pin in deploy/aws/terraform/services.tf
# (`external_image`) when this runs from a checkout of the repository. From
# CloudShell, with only this file uploaded, pass it explicitly and copy it
# from that same pin.
#
# WHY NOT `terraform apply`. Every service in services.tf has
# lifecycle { ignore_changes = [task_definition] }, so an apply registers a
# new task definition and leaves the running service on the old one. This
# does what cd.yml does for our own images: copy the SERVICE's current task
# definition, change only the image, register it, point the service at it.
#
# For hatchet (the engine) and redis. NOT for qdrant: Qdrant migrates its
# storage one minor version at a time and nothing else backs that data up,
# so it has its own script, upgrade-qdrant.sh, that snapshots first and
# checks point counts after every step. This one refuses to touch qdrant.
#
# Both services here run minimum-healthy 0%: the old task stops before the
# new one starts, so expect a gap of a minute or two. For hatchet that means
# workflow dispatch pauses; queued runs are picked up when it is back. The
# engine migrates its own schema on start, so rolling hatchet BACK may need
# its database restored too.
#
# First used for the hatchet-lite v0.86.12 -> v0.91.2 move (CVE-2026-61687)
# on 2026-09-24.
set -euo pipefail

CLUSTER="${ECS_CLUSTER:-georag}"
SERVICE=""
IMAGE=""
APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) if [ -z "$SERVICE" ]; then SERVICE="$arg"; elif [ -z "$IMAGE" ]; then IMAGE="$arg"; else
         echo "usage: bash $0 SERVICE [IMAGE] [--apply]" >&2; exit 2; fi ;;
  esac
done
[ -n "$SERVICE" ] || { echo "usage: bash $0 SERVICE [IMAGE] [--apply]" >&2; exit 2; }
CONTAINER="${CONTAINER:-$SERVICE}"

if [ "$SERVICE" = "qdrant" ]; then
  echo "qdrant has its own procedure (snapshots, one minor version per step): upgrade-qdrant.sh" >&2
  exit 2
fi

for tool in aws jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done

if [ -z "$IMAGE" ]; then
  tf="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../terraform/services.tf"
  if [ -f "$tf" ]; then
    IMAGE=$(awk -v s="$SERVICE" '
      /external_image = \{/ { inblock = 1; next }
      inblock && /^  \}/    { exit }
      inblock && $1 == s && $2 == "=" { gsub(/"/, "", $3); print $3; exit }' "$tf")
  fi
  if [ -z "$IMAGE" ]; then
    echo "no IMAGE given and no external_image pin for '$SERVICE' found in services.tf" >&2
    exit 2
  fi
  echo "image from services.tf external_image.$SERVICE"
fi

svc=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
        --query 'services[0].{td:taskDefinition,desired:desiredCount,running:runningCount}' --output json)
current_td=$(jq -r '.td // empty' <<<"$svc")
desired=$(jq -r .desired <<<"$svc")
running=$(jq -r .running <<<"$svc")
if [ -z "$current_td" ]; then
  echo "service '$SERVICE' not found in cluster '$CLUSTER'" >&2
  exit 1
fi

def=$(aws ecs describe-task-definition --task-definition "$current_td" --query taskDefinition)
old_image=$(jq -r --arg c "$CONTAINER" '.containerDefinitions[] | select(.name == $c) | .image' <<<"$def")
if [ -z "$old_image" ]; then
  echo "no container named '$CONTAINER' in $current_td (set CONTAINER=...)" >&2
  exit 1
fi

echo "service:        $CLUSTER/$SERVICE (desired $desired, running $running)"
echo "task def now:   $current_td"
echo "image now:      $old_image"
echo "image after:    $IMAGE"

if [ "$old_image" = "$IMAGE" ]; then
  echo "Already on that image. Nothing to do."
  exit 0
fi

if [ "$APPLY" -ne 1 ]; then
  echo
  echo "Dry run: nothing changed. Re-run with --apply to switch."
  exit 0
fi

new=$(jq --arg c "$CONTAINER" --arg img "$IMAGE" '
  (.containerDefinitions[] | select(.name == $c) | .image) = $img
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes,
        .compatibilities, .registeredAt, .registeredBy, .deregisteredAt)' <<<"$def")
new_td=$(aws ecs register-task-definition --cli-input-json "$new" \
           --query 'taskDefinition.taskDefinitionArn' --output text)
echo "registered:     $new_td"

aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
  --task-definition "$new_td" --query 'service.serviceName' --output text >/dev/null
echo "service now points at the new revision."
echo "rollback, if needed: aws ecs update-service --cluster $CLUSTER --service $SERVICE --task-definition $current_td"

if [ "$desired" = "0" ]; then
  echo
  echo "The service is scaled to 0 (the nightly stop). The new image starts when it is next scaled up."
  exit 0
fi

echo
echo "waiting for the new task to become stable (a few minutes)..."
if aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"; then
  echo "UPGRADE OK: $SERVICE is stable on $IMAGE"
else
  echo "the service did not stabilise; check the deployment in the ECS console and the" >&2
  echo "$SERVICE log stream in /ecs/georag. The circuit breaker rolls back automatically." >&2
  exit 1
fi
