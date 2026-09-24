#!/usr/bin/env bash
# Move the production Qdrant server to a newer version, one minor version at
# a time, with a snapshot of every collection taken first.
#
#   bash upgrade-qdrant.sh [HOP...]            # show what would change, change nothing
#   bash upgrade-qdrant.sh [HOP...] --apply    # snapshot, then step through each HOP
#
# Each HOP is a full image reference, digest-pinned, e.g.
#   qdrant/qdrant:v1.18.3@sha256:0bd9...  qdrant/qdrant:v1.19.1@sha256:1236...
# With no HOP, and when run from a checkout, the one hop is the pin in
# deploy/aws/terraform/services.tf (external_image.qdrant). Hops that are
# already behind the running version are skipped; a hop that jumps more than
# one minor version is refused.
#
# First used 2026-09-24 for v1.17.1 -> v1.18.3 -> v1.19.1, to match the
# qdrant-client 1.19.1 that fastapi and the hatchet worker run (the client
# warns when the server is more than one minor version behind).
#
# Why not `terraform apply`: every ECS service in deploy/aws/terraform/services.tf
# has lifecycle { ignore_changes = [task_definition] }, so an apply registers a
# new task definition and leaves the running service on the old one.
#
# Why one minor version per step: Qdrant migrates its on-disk storage one
# minor version at a time. Every collection's exact point count is checked
# after each step.
#
# Why a snapshot first: nothing else backs this data up (EFS automatic backups
# are not enabled). Every collection is snapshotted to the backups bucket
# before anything changes. The service stops the old task before starting the
# new one (minimum healthy 0%), so two versions never share the storage.
set -euo pipefail

CLUSTER="${ECS_CLUSTER:-georag}"
SERVICE="qdrant"
CONTAINER="qdrant"
APPLY=0
HOPS=()
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) HOPS+=("$arg") ;;
  esac
done

for tool in aws jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done

version_of () { local v="${1##*:v}"; echo "${v%%@*}"; }   # image ref -> 1.19.1
minor_of () { local v; v=$(version_of "$1"); v="${v#*.}"; echo "${v%%.*}"; }
major_of () { local v; v=$(version_of "$1"); echo "${v%%.*}"; }
newer_than () {   # $1 newer than $2, by version
  [ "$(version_of "$1")" != "$(version_of "$2")" ] &&
  [ "$(printf '%s\n%s\n' "$(version_of "$1")" "$(version_of "$2")" | sort -V | tail -1)" = "$(version_of "$1")" ]
}

if [ "${#HOPS[@]}" -eq 0 ]; then
  tf="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../terraform/services.tf"
  pin=""
  [ -f "$tf" ] && pin=$(awk '
      /external_image = \{/ { inblock = 1; next }
      inblock && /^  \}/    { exit }
      inblock && $1 == "qdrant" && $2 == "=" { gsub(/"/, "", $3); print $3; exit }' "$tf")
  [ -n "$pin" ] || { echo "no HOP given and no qdrant pin found in services.tf" >&2; exit 2; }
  HOPS=("$pin")
  echo "target from services.tf external_image.qdrant"
fi
for hop in "${HOPS[@]}"; do
  [[ "$(version_of "$hop")" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || { echo "cannot read a version from '$hop' (expected qdrant/qdrant:vX.Y.Z@sha256:...)" >&2; exit 2; }
  [[ "$hop" == *@sha256:* ]] \
    || { echo "'$hop' is not digest-pinned; a re-pushed tag could change what runs" >&2; exit 2; }
done
FINAL="${HOPS[${#HOPS[@]}-1]}"

svc=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
        --query 'services[0].{td:taskDefinition,desired:desiredCount,running:runningCount}' --output json)
current_td=$(jq -r .td <<<"$svc")
desired=$(jq -r .desired <<<"$svc")
running=$(jq -r .running <<<"$svc")
if [ -z "$current_td" ] || [ "$current_td" = "null" ]; then
  echo "service '$SERVICE' not found in cluster '$CLUSTER'" >&2
  exit 1
fi
def=$(aws ecs describe-task-definition --task-definition "$current_td" --query taskDefinition)
old_image=$(jq -r --arg c "$CONTAINER" '.containerDefinitions[] | select(.name == $c) | .image' <<<"$def")
if [ -z "$old_image" ]; then
  echo "no container named '$CONTAINER' in $current_td" >&2
  exit 1
fi

echo "service:        $CLUSTER/$SERVICE (desired $desired, running $running)"
echo "task def now:   $current_td"
echo "image now:      $old_image"
for hop in "${HOPS[@]}"; do echo "then:           $hop"; done

if [ "$old_image" = "$FINAL" ]; then
  echo "Already on v$(version_of "$FINAL"). Nothing to do."
  exit 0
fi
[[ "$(version_of "$old_image")" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || { echo "cannot read the running version from '$old_image' - stopping" >&2; exit 1; }

# Skip hops at or behind the running version; every remaining hop must be
# newer than the one before it and at most one minor version on from it.
START=0
prev="$old_image"
for i in "${!HOPS[@]}"; do
  hop="${HOPS[$i]}"
  if [ "$(major_of "$hop")" != "$(major_of "$old_image")" ]; then
    echo "a major-version change ($(version_of "$old_image") -> $(version_of "$hop")) is not a" >&2
    echo "minor-step upgrade; read Qdrant's release notes and do it by hand." >&2
    exit 1
  fi
  if ! newer_than "$hop" "$old_image"; then
    if [ "$prev" != "$old_image" ]; then
      echo "hops are out of order: $(version_of "$hop") comes after $(version_of "$prev")" >&2
      exit 1
    fi
    START=$((i + 1))
    echo "(already on $(version_of "$old_image") - skipping $(version_of "$hop"))"
    continue
  fi
  if ! newer_than "$hop" "$prev"; then
    echo "hops are out of order: $(version_of "$hop") comes after $(version_of "$prev")" >&2
    exit 1
  fi
  if [ "$(minor_of "$hop")" -gt $(( $(minor_of "$prev") + 1 )) ]; then
    echo "$(version_of "$prev") -> $(version_of "$hop") skips a minor version. Qdrant migrates" >&2
    echo "storage one minor version at a time: add the intermediate version as a HOP." >&2
    exit 1
  fi
  prev="$hop"
done
if [ "$START" -ge "${#HOPS[@]}" ]; then
  echo "Nothing newer than the running $(version_of "$old_image") in the hops given. Nothing to do."
  exit 0
fi

if [ "$APPLY" -ne 1 ]; then
  echo
  echo "Dry run: nothing changed. Re-run with --apply to snapshot and upgrade."
  exit 0
fi
if [ "$desired" = "0" ] || [ "$running" = "0" ]; then
  echo "Qdrant is scaled to 0 (the nightly stop). It has to be running to be" >&2
  echo "snapshotted - run this during the day." >&2
  exit 1
fi

# ── one-off tasks inside the VPC, on the fastapi task definition ───────────
NET=$(aws ecs describe-services --cluster "$CLUSTER" --services fastapi \
        --query 'services[0].networkConfiguration.awsvpcConfiguration')
SUBNETS=$(jq -r '.subnets|join(",")' <<<"$NET")
SG=$(jq -r '.securityGroups|join(",")' <<<"$NET")
if [ -z "$SUBNETS" ] || [ "$SUBNETS" = "null" ]; then
  echo "could not read the fastapi service's subnets" >&2
  exit 1
fi
NETCFG="awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}"

read -r -d '' QDRANT_PY <<'PYEOF' || true
import json, os, sys
import boto3, httpx

base = f"http://{os.environ['QDRANT_HOST']}:{os.environ.get('QDRANT_PORT', '6333')}"
headers = {"api-key": os.environ["QDRANT_API_KEY"]} if os.environ.get("QDRANT_API_KEY") else {}
client = httpx.Client(base_url=base, headers=headers, timeout=900)

version = client.get("/").json().get("version")
names = sorted(c["name"] for c in client.get("/collections").json()["result"]["collections"])
counts = {}
for name in names:
    r = client.post(f"/collections/{name}/points/count", json={"exact": True})
    r.raise_for_status()
    counts[name] = r.json()["result"]["count"]
print("QDRANT_STATE " + json.dumps({"version": version, "counts": counts}), flush=True)

if os.environ.get("QDRANT_MODE") == "snapshot":
    s3 = boto3.client("s3")
    bucket = os.environ["AWS_BUCKET_BACKUPS"]
    prefix = os.environ["SNAPSHOT_PREFIX"]
    for name in names:
        r = client.post(f"/collections/{name}/snapshots", params={"wait": "true"})
        r.raise_for_status()
        snap = r.json()["result"]["name"]
        path = f"/tmp/{snap}"
        with client.stream("GET", f"/collections/{name}/snapshots/{snap}") as resp:
            resp.raise_for_status()
            with open(path, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
        key = f"{prefix}/{name}/{snap}"
        s3.upload_file(path, bucket, key)
        size = os.path.getsize(path)
        os.remove(path)
        client.delete(f"/collections/{name}/snapshots/{snap}")
        print(f"QDRANT_SNAPSHOT s3://{bucket}/{key} ({size} bytes)", flush=True)
    print("QDRANT_SNAPSHOTS_DONE", flush=True)
PYEOF

run_qdrant_task () {   # $1 = count | snapshot; prints the task log, returns its exit code
  local overrides arn code
  overrides=$(jq -n --arg py "$QDRANT_PY" --arg mode "$1" --arg prefix "${SNAPSHOT_PREFIX:-}" \
    '{containerOverrides:[{name:"fastapi",command:["python3","-c",$py],
      environment:[{name:"QDRANT_MODE",value:$mode},{name:"SNAPSHOT_PREFIX",value:$prefix}]}]}')
  arn=$(aws ecs run-task --cluster "$CLUSTER" --task-definition georag-fastapi \
    --launch-type FARGATE --network-configuration "$NETCFG" \
    --overrides "$overrides" --query 'tasks[0].taskArn' --output text)
  echo "$1 task: $arn" >&2
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$arn" >&2
  TASK_LOG=$(aws logs get-log-events --log-group-name /ecs/georag \
    --log-stream-name "fastapi/fastapi/${arn##*/}" \
    --query 'events[].message' --output text 2>/dev/null | tr '\t' '\n' || true)
  printf '%s\n' "$TASK_LOG" | grep -E '^QDRANT_|Error|Traceback' >&2 || true
  code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$arn" \
           --query 'tasks[0].containers[0].exitCode' --output text)
  [ "$code" = "0" ]
}

state_json () { printf '%s\n' "$TASK_LOG" | sed -n 's/^QDRANT_STATE //p' | tail -1; }

# ── 1. snapshot every collection to S3 ─────────────────────────────────────
SNAPSHOT_PREFIX="_ops/qdrant-snapshots/$(date -u +%Y%m%dT%H%M%SZ)-before-v$(version_of "$FINAL")"
echo
echo "=== 1. snapshot every collection -> backups bucket/$SNAPSHOT_PREFIX"
if ! run_qdrant_task snapshot || ! grep -q '^QDRANT_SNAPSHOTS_DONE' <<<"$TASK_LOG"; then
  echo "SNAPSHOT FAILED - nothing was upgraded." >&2
  exit 1
fi
BASELINE=$(state_json | jq -c .counts)
echo "baseline point counts: $BASELINE"

# ── 2. step through the minor versions ─────────────────────────────────────
roll_back_hint () {
  echo >&2
  echo "To go back to where this started:" >&2
  echo "  aws ecs update-service --cluster $CLUSTER --service $SERVICE --task-definition $current_td" >&2
  echo "An older Qdrant cannot always read storage a newer one migrated. If it" >&2
  echo "will not start, restore the snapshots under $SNAPSHOT_PREFIX in the" >&2
  echo "backups bucket (PUT /collections/<name>/snapshots/recover with a" >&2
  echo "presigned URL) - ask before doing this." >&2
}

base_def="$def"
step=0
for hop in "${HOPS[@]:$START}"; do
  step=$((step + 1))
  want=$(version_of "$hop")
  echo
  echo "=== 2.$step  move to v$want"
  new=$(jq --arg c "$CONTAINER" --arg img "$hop" '
    (.containerDefinitions[] | select(.name == $c) | .image) = $img
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes,
          .compatibilities, .registeredAt, .registeredBy, .deregisteredAt)' <<<"$base_def")
  new_td=$(aws ecs register-task-definition --cli-input-json "$new" \
             --query 'taskDefinition.taskDefinitionArn' --output text)
  echo "registered:     $new_td"
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --task-definition "$new_td" --query 'service.serviceName' --output text >/dev/null
  echo "waiting for v$want to become stable (a few minutes)..."
  if ! aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"; then
    echo "v$want did not stabilise; check the qdrant log stream in /ecs/georag." >&2
    roll_back_hint
    exit 1
  fi
  if ! run_qdrant_task count; then
    echo "could not read Qdrant after moving to v$want." >&2
    roll_back_hint
    exit 1
  fi
  got_version=$(state_json | jq -r .version)
  got_counts=$(state_json | jq -c .counts)
  echo "server reports: v$got_version   counts: $got_counts"
  if [ "$got_version" != "$want" ]; then
    echo "expected the server to report v$want - stopping." >&2
    roll_back_hint
    exit 1
  fi
  if [ "$(jq -S . <<<"$got_counts")" != "$(jq -S . <<<"$BASELINE")" ]; then
    echo "POINT COUNTS CHANGED (before: $BASELINE) - stopping." >&2
    roll_back_hint
    exit 1
  fi
  base_def=$(aws ecs describe-task-definition --task-definition "$new_td" --query taskDefinition)
done

echo
echo "QDRANT UPGRADE OK: $SERVICE is on v$(version_of "$FINAL") with every collection's point count unchanged."
echo "snapshots kept in the backups bucket under $SNAPSHOT_PREFIX"
