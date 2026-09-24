#!/usr/bin/env bash
# Rotate HATCHET_CLIENT_TOKEN on the AWS deployment.
#
#   bash rotate-hatchet-token.sh                 # preview: consumers + the current token's claims
#   bash rotate-hatchet-token.sh --apply         # mint a new token, store it, restart the consumers
#   bash rotate-hatchet-token.sh --rollback      # put the previous token back
#
# First run for real on 2026-09-24, from CloudShell.
#
# Hatchet tokens are independent JWTs, so both stay valid: roll the consumers
# onto the new one, check the worker still runs jobs. The OLD token is not
# revoked by this script - Hatchet refuses to let an API token list or revoke
# tokens (api/v1/server/authz: "bearer tokens cannot read, list, or write
# other bearer tokens"); only a signed-in dashboard user can. Unrevoked, it
# stops working at its own expiry, which the preview prints and --apply
# records in $STATE.
#
# The token itself is never printed. The mint task writes it to its own
# CloudWatch log stream (that is the only output hatchet-admin has); this
# script reads it from there and deletes that stream straight away, so the
# new token does not sit in the logs the way the first one did.
#
# Minting follows deploy/aws/README.md ("Minting the real one"), which was
# run for real on 2026-09-18: `--config /config` before the subcommand, and
# SERVER_AUTH_COOKIE_SECRETS set.
set -euo pipefail

CLUSTER="${ECS_CLUSTER:-georag}"
LOG_GROUP="/ecs/georag"
STATE="$HOME/.hatchet-token-rotation"      # the OLD token's id and expiry - no secrets
MODE="${1:-preview}"

for tool in aws jq base64; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done

claims () {   # stdin: a JWT -> its payload as JSON (signature not checked; this is our own token)
  local p
  p=$(cut -d. -f2 | tr '_-' '/+')
  while [ $(( ${#p} % 4 )) -ne 0 ]; do p="$p="; done
  printf '%s' "$p" | base64 -d 2>/dev/null
}

net_config () {
  local net subnets sg
  net=$(aws ecs describe-services --cluster "$CLUSTER" --services fastapi \
          --query 'services[0].networkConfiguration.awsvpcConfiguration')
  subnets=$(jq -r '.subnets|join(",")' <<<"$net")
  sg=$(jq -r '.securityGroups|join(",")' <<<"$net")
  [ -n "$subnets" ] && [ "$subnets" != "null" ] || { echo "could not read fastapi's subnets" >&2; exit 1; }
  echo "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sg],assignPublicIp=DISABLED}"
}

# Every service whose task definition injects HATCHET_CLIENT_TOKEN. Discovered,
# not listed, so a service added later is not silently left on the old token.
consumers () {
  local arns svc td
  arns=$(aws ecs list-services --cluster "$CLUSTER" --query 'serviceArns[]' --output text)
  for svc in $arns; do
    td=$(aws ecs describe-services --cluster "$CLUSTER" --services "$svc" \
           --query 'services[0].taskDefinition' --output text)
    if aws ecs describe-task-definition --task-definition "$td" \
         --query 'taskDefinition.containerDefinitions[].secrets[].name' --output text \
         | tr '\t' '\n' | grep -qx HATCHET_CLIENT_TOKEN; then
      echo "${svc##*/}"
    fi
  done
}

secret_arn () {   # the Secrets Manager secret the services read the token from
  local td
  td=$(aws ecs describe-services --cluster "$CLUSTER" --services fastapi \
         --query 'services[0].taskDefinition' --output text)
  aws ecs describe-task-definition --task-definition "$td" \
    --query "taskDefinition.containerDefinitions[].secrets[?name=='HATCHET_CLIENT_TOKEN'].valueFrom" \
    --output text | head -1 | sed 's/:HATCHET_CLIENT_TOKEN::$//'
}

restart_consumers () {
  local svc
  for svc in "$@"; do
    aws ecs update-service --cluster "$CLUSTER" --service "$svc" --force-new-deployment \
      --query 'service.serviceName' --output text >/dev/null
    echo "  restarting $svc"
  done
  echo "waiting for all of them to become stable (several minutes)..."
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$@"
}

describe_token () {   # $1 label, stdin JWT
  local c
  c=$(claims)
  jq -r --arg l "$1" '"\($l): tenant \(.sub // "?")  token_id \(.token_id // "(none in claims)")  expires \(if .exp then (.exp|todate) else "never" end)"' <<<"$c"
}

SECRET_ARN=$(secret_arn)
[ -n "$SECRET_ARN" ] || { echo "no HATCHET_CLIENT_TOKEN secret on the fastapi task definition" >&2; exit 1; }
mapfile -t CONSUMERS < <(consumers)

case "$MODE" in
# ─────────────────────────────────────────────────────────────────────────────
preview)
  echo "secret:     $SECRET_ARN (key HATCHET_CLIENT_TOKEN)"
  echo "consumers:  ${CONSUMERS[*]}"
  aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query SecretString --output text \
    | jq -r '.HATCHET_CLIENT_TOKEN' | describe_token "current token"
  echo
  echo "Nothing changed. Next: bash $0 --apply"
  ;;

# ─────────────────────────────────────────────────────────────────────────────
--apply)
  CURRENT_JSON=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query SecretString --output text)
  OLD_TOKEN=$(jq -r '.HATCHET_CLIENT_TOKEN' <<<"$CURRENT_JSON")
  OLD_CLAIMS=$(printf '%s' "$OLD_TOKEN" | claims)
  TENANT=$(jq -r '.sub // empty' <<<"$OLD_CLAIMS")
  [ -n "$TENANT" ] || { echo "the current token carries no tenant (sub) claim - stopping" >&2; exit 1; }
  printf '%s' "$OLD_TOKEN" | describe_token "old token"

  NAME="ecs-$(date -u +%Y%m%dT%H%M%SZ)"
  NETCFG=$(net_config)
  echo
  echo "=== 1. mint a new token ($NAME) for tenant $TENANT"
  OVR=$(jq -n --arg t "$TENANT" --arg n "$NAME" '{containerOverrides:[{name:"hatchet",
          command:["/hatchet-admin","--config","/config","token","create","--name",$n,"--tenant-id",$t],
          environment:[{name:"SERVER_AUTH_COOKIE_SECRETS",value:"mint mint"}]}]}')
  TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition georag-hatchet --launch-type FARGATE \
           --network-configuration "$NETCFG" --overrides "$OVR" --query 'tasks[0].taskArn' --output text)
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
  STREAM="hatchet/hatchet/${TASK##*/}"
  NEW=$(aws logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "$STREAM" \
          --query 'events[].message' --output text 2>/dev/null \
        | tr '\t' '\n' | grep -oE 'ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+' | tail -1 || true)
  # The stream held the new token in plain text. Gone before anything else.
  aws logs delete-log-stream --log-group-name "$LOG_GROUP" --log-stream-name "$STREAM" 2>/dev/null \
    && echo "deleted the mint task's log stream" \
    || echo "WARNING: could not delete log stream $STREAM - delete it by hand" >&2
  if [ -z "$NEW" ]; then
    echo "no token was minted. Nothing was changed. The task's exit:" >&2
    aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" \
      --query 'tasks[0].{exit:containers[0].exitCode,reason:stoppedReason}' --output json >&2
    exit 1
  fi
  NEW_CLAIMS=$(printf '%s' "$NEW" | claims)
  if [ "$(jq -r '.sub // empty' <<<"$NEW_CLAIMS")" != "$TENANT" ]; then
    echo "the minted token is for a different tenant - not storing it" >&2
    exit 1
  fi
  if [ "$NEW" = "$OLD_TOKEN" ]; then echo "minted token equals the old one?! stopping" >&2; exit 1; fi
  printf '%s' "$NEW" | describe_token "new token"

  echo
  echo "=== 2. store it in Secrets Manager (the old value stays as AWSPREVIOUS)"
  # The token reaches jq through the environment, not argv (readable by any
  # process on the machine), and the CLI through stdin.
  T="$NEW" jq -c '.HATCHET_CLIENT_TOKEN = env.T' <<<"$CURRENT_JSON" \
    | aws secretsmanager put-secret-value --secret-id "$SECRET_ARN" --secret-string file:///dev/stdin \
        --query VersionId --output text >/dev/null
  unset NEW CURRENT_JSON
  jq -n --arg id "$(jq -r '.token_id // ""' <<<"$OLD_CLAIMS")" --arg name "$NAME" \
        --arg tenant "$TENANT" --arg at "$(date -u +%FT%TZ)" \
        --arg exp "$(jq -r 'if .exp then (.exp|todate) else "never" end' <<<"$OLD_CLAIMS")" \
        '{old_token_id:$id, old_expires:$exp, new_token_name:$name, tenant:$tenant, rotated_at:$at}' > "$STATE"
  unset OLD_TOKEN
  echo "stored. (old token id + expiry saved to $STATE)"

  echo
  echo "=== 3. restart everything that reads it: ${CONSUMERS[*]}"
  restart_consumers "${CONSUMERS[@]}"
  # Only after every new task is up: an old worker finishing a step while it
  # drained would otherwise count as proof the new token works.
  SINCE=$(( $(date +%s) * 1000 ))

  echo
  echo "=== 4. is the worker running jobs on the new token?"
  # A worker on a bad token does not crash; it sits Running and picks up
  # nothing. The crons fire every few minutes, so a finished step proves it.
  for _ in $(seq 1 24); do
    if aws logs filter-log-events --log-group-name "$LOG_GROUP" --log-stream-name-prefix hatchet-worker/ \
         --start-time "$SINCE" --filter-pattern '"finished step run"' --max-items 1 \
         --query 'events[0].message' --output text 2>/dev/null | grep -q 'finished step run'; then
      echo "yes - the worker finished a step after the restart."
      echo
      echo "TOKEN ROTATED. Everything now uses the new token."
      echo "The old one ($(jq -r .old_token_id "$STATE")) is not revoked; it expires $(jq -r .old_expires "$STATE")."
      echo "(To undo:  bash $0 --rollback)"
      exit 0
    fi
    sleep "${WORKER_POLL_SECONDS:-15}"   # 24 x 15s = 6 minutes; the tests set 0
  done
  echo "the worker has not finished a step in 6 minutes. Check it, or undo with: bash $0 --rollback" >&2
  exit 1
  ;;

# ─────────────────────────────────────────────────────────────────────────────
--rollback)
  # Only the token key is restored. AWSPREVIOUS is the whole JSON as it was
  # before the LAST write to the secret; if anything else wrote the secret
  # since (a new key, another rotation), putting that whole version back would
  # silently undo that too.
  echo "putting the previous HATCHET_CLIENT_TOKEN back (from Secrets Manager AWSPREVIOUS)"
  PREV_TOKEN=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --version-stage AWSPREVIOUS \
                 --query SecretString --output text | jq -r '.HATCHET_CLIENT_TOKEN // empty')
  CURRENT_JSON=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query SecretString --output text)
  if [ -z "$PREV_TOKEN" ]; then
    echo "the previous secret version holds no HATCHET_CLIENT_TOKEN - nothing to roll back to" >&2
    exit 1
  fi
  if [ "$PREV_TOKEN" = "$(jq -r '.HATCHET_CLIENT_TOKEN // empty' <<<"$CURRENT_JSON")" ]; then
    echo "the previous secret version holds the SAME token as the current one: something" >&2
    echo "else wrote the secret after the rotation. Nothing changed." >&2
    exit 1
  fi
  printf '%s' "$PREV_TOKEN" | describe_token "restoring"
  T="$PREV_TOKEN" jq -c '.HATCHET_CLIENT_TOKEN = env.T' <<<"$CURRENT_JSON" \
    | aws secretsmanager put-secret-value --secret-id "$SECRET_ARN" --secret-string file:///dev/stdin \
        --query VersionId --output text >/dev/null
  unset PREV_TOKEN CURRENT_JSON
  echo "stored. Restarting: ${CONSUMERS[*]}"
  restart_consumers "${CONSUMERS[@]}"
  rm -f "$STATE"
  echo "ROLLED BACK. The new token still exists but nothing uses it."
  ;;

*)
  echo "usage: bash $0 [--apply | --rollback]" >&2
  exit 2
  ;;
esac
