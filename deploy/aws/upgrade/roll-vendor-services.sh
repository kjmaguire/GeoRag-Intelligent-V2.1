#!/usr/bin/env bash
# Put hatchet, redis and qdrant onto the task definition `terraform apply`
# last registered for them.
#
#   bash roll-vendor-services.sh [SERVICE...]            # show what would change
#   bash roll-vendor-services.sh [SERVICE...] --apply    # do it
#
# SERVICE defaults to all three. Run it straight after any `terraform apply`
# that touched their task definitions (an env var, a secret, a mount, a stop
# timeout, a new image pin). cd.yml's "Vendor services on their latest task
# definition?" step warns on every deploy until it has been run.
#
# WHY THIS EXISTS (audit AWS-2, 2026-09-29). Every service in services.tf has
# lifecycle { ignore_changes = [task_definition] }. That is right for the
# seven services CD rolls — CD owns their running revision, and Terraform
# must not drag them back to `var.image_tag`. But nothing rolls the three
# vendor services, so for them an apply registers a new revision and leaves
# the service on the old one, indefinitely, while the apply reports success.
# `--force-new-deployment` does not help: it restarts the service's CURRENT
# revision, not the latest.
#
# WHY A SCRIPT AND NOT A SECOND aws_ecs_service RESOURCE WITHOUT THE IGNORE.
# That was the other option, and it is worse for exactly one of the three:
# Terraform would then roll qdrant onto a new image as a side effect of any
# apply, skipping upgrade-qdrant.sh's snapshot and its one-minor-version-per-
# hop rule — and Qdrant's data has no other backup. It would also silently
# revert every revision upgrade-service-image.sh registers out of band. So the
# ignore stays uniform and this script is the explicit, reviewed roll:
#
#   * hatchet, redis — rolled to the family's latest ACTIVE revision.
#   * qdrant         — rolled ONLY when the image is unchanged (env, secrets,
#                      stop timeout). An image change is refused and pointed
#                      at upgrade-qdrant.sh, which snapshots first.
#
# All three run minimum-healthy 0%: the old task stops before the new one
# starts, so each roll is a gap of a minute or two (Hatchet dispatch pauses,
# Redis-backed queues and sessions wait, retrieval returns nothing). Run it in
# a quiet moment. A service at desired 0 (the nightly stop) is re-pointed and
# picks the new revision up at the morning start.
set -euo pipefail

CLUSTER="${ECS_CLUSTER:-georag}"
PREFIX="${SERVICE_FAMILY_PREFIX:-georag}"
APPLY=0
SERVICES=()
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    hatchet|redis|qdrant) SERVICES+=("$arg") ;;
    *) echo "not a vendor service: $arg (hatchet | redis | qdrant). CD rolls the other seven." >&2; exit 2 ;;
  esac
done
[ "${#SERVICES[@]}" -gt 0 ] || SERVICES=(hatchet redis qdrant)

for tool in aws jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done

image_of() { jq -r --arg c "$2" '.containerDefinitions[] | select(.name == $c) | .image' <<<"$1"; }

REFUSED=()
ROLLED=()
FAILED=()

for svc in "${SERVICES[@]}"; do
  echo "── ${svc}"
  info=$(aws ecs describe-services --cluster "$CLUSTER" --services "$svc" \
           --query 'services[0].{td:taskDefinition,desired:desiredCount,running:runningCount}' --output json)
  running_td=$(jq -r '.td // empty' <<<"$info")
  desired=$(jq -r '.desired // 0' <<<"$info")
  if [ -z "$running_td" ]; then
    echo "   service not found in cluster ${CLUSTER}" >&2
    FAILED+=("$svc: not found")
    continue
  fi

  # The family name resolves to the latest ACTIVE revision — which, straight
  # after an apply, is the one Terraform just registered.
  latest=$(aws ecs describe-task-definition --task-definition "${PREFIX}-${svc}" --query taskDefinition)
  latest_td=$(jq -r '.taskDefinitionArn' <<<"$latest")

  if [ "${running_td##*/}" = "${latest_td##*/}" ]; then
    echo "   up to date on ${latest_td##*/}"
    continue
  fi

  current=$(aws ecs describe-task-definition --task-definition "$running_td" --query taskDefinition)
  old_image=$(image_of "$current" "$svc")
  new_image=$(image_of "$latest" "$svc")
  echo "   running:  ${running_td##*/}  ${old_image}"
  echo "   latest:   ${latest_td##*/}  ${new_image}"

  if [ "$svc" = "qdrant" ] && [ "$old_image" != "$new_image" ]; then
    echo "   REFUSED: the latest qdrant revision changes the IMAGE. Qdrant migrates its"
    echo "   storage one minor version at a time and nothing else backs it up, so image"
    echo "   moves go through upgrade-qdrant.sh (snapshot first). Run that, then re-run this."
    REFUSED+=("qdrant")
    continue
  fi
  if [ "$svc" = "hatchet" ] && [ "$old_image" != "$new_image" ]; then
    echo "   note: the engine migrates its own schema on start, so rolling it BACK later"
    echo "   may need its database restored too (upgrade-service-image.sh says the same)."
  fi

  if [ "$APPLY" -ne 1 ]; then
    echo "   dry run: would point ${svc} at ${latest_td##*/}"
    continue
  fi

  aws ecs update-service --cluster "$CLUSTER" --service "$svc" \
    --task-definition "$latest_td" --query 'service.serviceName' --output text >/dev/null
  echo "   now points at ${latest_td##*/}"
  echo "   rollback, if needed: aws ecs update-service --cluster $CLUSTER --service $svc --task-definition $running_td"
  if [ "$desired" = "0" ]; then
    echo "   scaled to 0 (nightly stop): the new revision starts at the next scale-up"
    ROLLED+=("$svc")
    continue
  fi
  if aws ecs wait services-stable --cluster "$CLUSTER" --services "$svc"; then
    echo "   stable"
    ROLLED+=("$svc")
  else
    echo "   did NOT stabilise — the circuit breaker rolls back by itself; read the $svc stream in /ecs/georag" >&2
    FAILED+=("$svc: did not stabilise")
  fi
done

echo
[ "${#ROLLED[@]}" -eq 0 ] || echo "rolled: ${ROLLED[*]}"
[ "$APPLY" -eq 1 ] || echo "Dry run: nothing changed. Re-run with --apply to roll."
if [ "${#REFUSED[@]}" -gt 0 ] || [ "${#FAILED[@]}" -gt 0 ]; then
  [ "${#REFUSED[@]}" -eq 0 ] || echo "refused: ${REFUSED[*]}" >&2
  [ "${#FAILED[@]}" -eq 0 ] || printf 'failed: %s\n' "${FAILED[@]}" >&2
  exit 1
fi
exit 0
