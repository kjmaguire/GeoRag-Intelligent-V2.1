#!/usr/bin/env bash
# rotate-app-key.sh — rotate Laravel's APP_KEY on the ECS Fargate
# deployment.
#
#   bash deploy/aws/rotation/rotate-app-key.sh           # dry run: preflight + plan
#   bash deploy/aws/rotation/rotate-app-key.sh --apply   # do it
#   bash deploy/aws/rotation/rotate-app-key.sh --finish  # promote + roll only, after a run
#                                                        # whose rotation task succeeded but
#                                                        # which failed before every service
#                                                        # had the new key
#
# APP_KEY encrypts query_audit_log.query_text / response_text and keys
# query_text_hash (docs/RUNBOOK.md § "APP_KEY rotation checklist").
# Rotating it is mint → stage → quiesce → re-encrypt → promote → roll.
#
# Successor to deploy/azure/containerapps/rotate-app-key.sh, deleted with
# the Azure tree on 2026-09-08 and recorded in ops/runbooks/secret-rotation.md
# §2 as an unreplaced capability loss. Read that section's findings first:
# most are about Laravel and the audit ledger and still bind; three were
# consequences of running inside a live Azure replica and are gone.
# rotate-app-key-inside.sh's header says which, and why.
#
# ---------------------------------------------------------------------
# THE CONTRACT, WHICH IS THE PART THAT DID NOT SIMPLIFY
# ---------------------------------------------------------------------
#   - no secret changes anywhere before the rotation task reports success;
#   - a dump failure is fully reversible and IS reversed here; a restore
#     failure is not, and must be followed by neither traffic nor a
#     rollback of the secret;
#   - one service failing to roll does not stop the others, for the same
#     reason the nightly sweeps are not `set -e`;
#   - the key never reaches the terminal, a log, or an API parameter.
#
# ---------------------------------------------------------------------
# WHAT IS DIFFERENT FROM AZURE, BEYOND THE CLI
# ---------------------------------------------------------------------
# 1. THE SERVING TASKS ARE STOPPED, NOT PUT IN MAINTENANCE MODE.
#    config/app.php:121 leaves APP_MAINTENANCE_DRIVER at `file`, so
#    maintenance state lives on one container's filesystem. Azure had
#    exactly one octane replica and ran the rotation inside it, so `php
#    artisan down` there covered every writer. laravel-octane runs TWO
#    tasks here (main.tf:52) and the rotation runs in neither of them, so
#    there is no container in which `down` would mean anything. Scaling to
#    zero is the only form of "stopped writing" available here, and the
#    only one that can be verified from outside rather than inferred.
#
# 2. HORIZON IS STOPPED TOO, AND THE AZURE RUNBOOK WAS WRONG ABOUT THIS.
#    secret-rotation.md §2 finding 5 said "Horizon does not need pausing.
#    Only the Octane query controller writes query_audit_log; Horizon only
#    reads." It does not: app/Jobs/StreamQueryFromFastApi.php writes
#    `response_text` — an `encrypted` column — on the completion path
#    (:418), the error path (:489-493) and the failed path (:538+). An
#    in-flight stream job finalising a row under the OLD key after the dump
#    had read it would leave that row unreadable after the promote, with
#    nothing to notice. That was true on Azure too; it was never a property
#    of the cloud. Corrected in the runbook, and both services are scaled
#    to zero here.
#
# 3. THE RECOVERY ASSET IS AN RDS SNAPSHOT, NOT A FILE ON A CONTAINER.
#    The Azure design's dump survived a failed restore because the replica
#    survived it. A Fargate task's disk does not. And a half-rotated table
#    cannot be re-dumped — audit:dump-pii reads through the `encrypted`
#    cast and DumpAuditPii.php:186 fails the whole dump on the first row it
#    cannot decrypt. So this script takes an explicit snapshot first and
#    refuses to proceed without one. Heavier to use than re-running a
#    restore by hand; unlike that, it always works.
#
# 4. THE KEY NEVER REACHES THIS MACHINE'S TERMINAL, AND --finish NEEDS
#    NOTHING FROM IT. Azure minted the key inside the replica, read it back
#    over stdout, and wrote it 0600 to the operator's laptop because a
#    shell variable was otherwise its only copy. Here it is minted locally,
#    written straight into Secrets Manager as APP_KEY_NEXT, and injected
#    into the rotation task from there (terraform/rotation.tf). It is never
#    echoed, never an argv, never a RunTask parameter. --finish reads
#    everything it needs from Secrets Manager, so a dropped SSH session
#    costs nothing — where Azure's needed ROTATE_NEWKEY exported from a file.
#
# ---------------------------------------------------------------------
# THE ONE THING THIS DESIGN MAKES WORSE, STATED
# ---------------------------------------------------------------------
# The app secret is a single JSON document (config.tf), so changing one key
# is a read-modify-write of ALL of them. For the moments it takes, this
# script holds FASTAPI_SERVICE_KEY, QDRANT_API_KEY, REDIS_PASSWORD,
# HATCHET_CLIENT_TOKEN, FLOW_JWT_SECRET and REVERB_APP_SECRET in a 0600
# temp file. That is a property of the secret's shape, not of the rotation;
# it is why every write below goes through `file://` rather than an argv,
# and why the temp files are shredded on every exit path including a signal.
set -uo pipefail
set +x   # never trace: the new key and the whole secret document are below

CLUSTER="${ROTATE_CLUSTER:-georag}"
DB_INSTANCE="${ROTATE_DB_INSTANCE:-georag-pg}"
SECRET_ID="${ROTATE_SECRET_ID:-georag/app}"
TASK_FAMILY="${ROTATE_TASK_FAMILY:-georag-app-key-rotation}"
SUBNETS="${ROTATE_SUBNETS:-}"                # terraform output private_subnet_ids
SECURITY_GROUP="${ROTATE_SECURITY_GROUP:-}"  # terraform output task_security_group_id

# Every Laravel service reads APP_KEY and must end up on the new one.
# QUIESCE is the subset that WRITES encrypted columns and therefore has to
# be at zero tasks while the ledger is re-encrypted; ROLL is every service
# that has to restart to pick the new key up. Reverb neither reads nor
# writes query_audit_log, so it is rolled but not quiesced — stopping it
# would drop every open WebSocket for no benefit.
QUIESCE="${ROTATE_QUIESCE:-laravel-octane laravel-horizon}"
ROLL="${ROTATE_ROLL:-laravel-octane laravel-horizon laravel-reverb}"

WAIT_TRIES="${ROTATE_WAIT_TRIES:-60}"
WAIT_INTERVAL="${ROTATE_WAIT_INTERVAL:-10}"
STAMP="$(date -u +%Y%m%d%H%M%S)"
SNAPSHOT_ID="${ROTATE_SNAPSHOT_ID:-${DB_INSTANCE}-preappkey-${STAMP}}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSIDE="${ROTATE_INSIDE:-${HERE}/rotate-app-key-inside.sh}"

MODE=dry
case "${1:-}" in
  --apply)  MODE=apply ;;
  --finish) MODE=finish ;;
  "")       MODE=dry ;;
  *) echo "usage: $0 [--apply|--finish]" >&2; exit 2 ;;
esac

say() { printf '# %s\n' "$*" >&2; }
die() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }

# Every temp file that can hold a secret is registered here and shredded on
# every exit path. TMPFILE is a global set by mktemp_secret rather than a
# return value on purpose: `f=$(mktemp_secret)` would run the function in a
# SUBSHELL, so the registration would be discarded and nothing would ever
# be shredded — a leak that looks exactly like working code.
SCRATCH=""
TMPFILE=""
cleanup() {
  [ -n "$SCRATCH" ] || return 0
  # shellcheck disable=SC2086  # deliberate word splitting: SCRATCH is a list
  shred -u $SCRATCH 2>/dev/null || rm -f $SCRATCH 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM

mktemp_secret() {
  TMPFILE="$(umask 077; mktemp)" || return 1
  SCRATCH="${SCRATCH} ${TMPFILE}"
  return 0
}

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is not on PATH. $2"; }
need aws "Install the AWS CLI v2."
need jq  "The app secret is one JSON document; every single-key edit is a jq read-modify-write."
need openssl "Used to mint the key locally, so it never transits a container's stdout."
[ -f "$INSIDE" ] || die "${INSIDE} not found next to this script."

# ---------------------------------------------------------------- helpers ---

# Reads the whole app secret into a fresh 0600 file (path in TMPFILE). The
# document never becomes a shell variable and never reaches an argv.
read_secret_to_file() {
  mktemp_secret || return 1
  aws secretsmanager get-secret-value --secret-id "$SECRET_ID" \
      --query SecretString --output text > "$TMPFILE" 2>/dev/null || return 1
  jq -e type >/dev/null 2>&1 < "$TMPFILE" || return 1
  return 0
}

write_secret_from_file() {
  aws secretsmanager put-secret-value --secret-id "$SECRET_ID" \
      --secret-string "file://$1" --output none 2>/dev/null
}

secret_has_key() { jq -e --arg k "$1" 'has($k)' >/dev/null 2>&1 < "$2"; }

# Rewrites the secret by piping it through `jq $1`, and leaves the new
# document's path in TMPFILE. Used for every staging, promotion and cleanup
# write so none of them is a separate chance to get the file handling wrong.
rewrite_secret() {
  local filter="$1" src="$2" out
  mktemp_secret || return 1
  out="$TMPFILE"
  jq "$filter" < "$src" > "$out" || return 1
  write_secret_from_file "$out" || return 1
  TMPFILE="$out"
  return 0
}

desired_count() {
  aws ecs describe-services --cluster "$CLUSTER" --services "$1" \
      --query 'services[0].desiredCount' --output text 2>/dev/null
}
running_count() {
  aws ecs describe-services --cluster "$CLUSTER" --services "$1" \
      --query 'services[0].runningCount' --output text 2>/dev/null
}

# Waits for runningCount to actually reach $2. The sweeps' contract applies
# here too: judge the state, not the exit code of the thing that asked for
# it. `wait services-stable` runs first because it is cheap and correct when
# it works, but its success is not what this returns on.
wait_running() {
  local svc="$1" want="$2" i=0 have=""
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$svc" >/dev/null 2>&1
  while [ "$i" -lt "$WAIT_TRIES" ]; do
    have="$(running_count "$svc")"
    [ "$have" = "$want" ] && return 0
    i=$((i + 1)); sleep "$WAIT_INTERVAL"
  done
  printf '%s' "${have:-unknown}"
  return 1
}

# Restores each "service:count" pair. Never stops at the first failure: a
# service left at zero is an outage, and the remaining ones are not made
# better by abandoning them.
restore_counts() {
  local e rc=0
  for e in $1; do
    aws ecs update-service --cluster "$CLUSTER" --service "${e%%:*}" \
        --desired-count "${e#*:}" --output none 2>/dev/null \
      || { say "WARNING: could not restore ${e%%:*} to desired ${e#*:} — do it by hand."; rc=1; }
  done
  return $rc
}

# Runs a shell script inside a one-off rotation task. The script is base64'd
# into a command override; it carries no secrets — both keys arrive through
# the task definition — so it is safe in the CloudTrail record of the
# RunTask call.
#
# Results come back in globals, NOT on stdout, for the same reason
# mktemp_secret does: `rc=$(run_rotation_task ...)` would run this in a
# SUBSHELL, and every global it set — the task ARN the operator needs in
# order to go and read what happened — would be discarded on return. The
# error paths below print that ARN, so getting this wrong produces recovery
# instructions with a blank in the middle of them.
#
# Returns: 0 the task ran and stopped (LAST_TASK_EXIT holds its exit code)
#          1 the task could not be started at all
#          2 it started but did not stop within the waiter's budget
LAST_TASK_ARN=""
LAST_TASK_REASON=""
LAST_TASK_EXIT=""
run_rotation_task() {
  local script_file="$1" b64 overrides arn
  LAST_TASK_ARN=""; LAST_TASK_REASON=""; LAST_TASK_EXIT=""

  b64="$(base64 -w0 < "$script_file" 2>/dev/null || base64 < "$script_file" | tr -d '\n')"
  overrides="$(jq -nc --arg b64 "$b64" \
    '{containerOverrides: [{name: "app-key-rotation", command: ["echo \($b64) | base64 -d | bash"]}]}')"

  arn="$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TASK_FAMILY" \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SECURITY_GROUP}],assignPublicIp=DISABLED}" \
      --overrides "$overrides" \
      --query 'tasks[0].taskArn' --output text 2>/dev/null)" || arn=""
  if [ -z "$arn" ] || [ "$arn" = "None" ]; then
    return 1
  fi
  LAST_TASK_ARN="$arn"
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$arn" 2>/dev/null || return 2
  LAST_TASK_EXIT="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$arn" \
        --query 'tasks[0].containers[0].exitCode' --output text 2>/dev/null)"
  LAST_TASK_REASON="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$arn" \
        --query 'tasks[0].stoppedReason' --output text 2>/dev/null)"
  return 0
}

# --------------------------------------------------------------- preflight ---
aws sts get-caller-identity --output none 2>/dev/null \
  || die "no usable AWS credentials in this shell."

DB_STATE="$(aws rds describe-db-instances --db-instance-identifier "$DB_INSTANCE" \
            --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null)"
[ "$DB_STATE" = "available" ] || {
  echo "ABORT: ${DB_INSTANCE} is '${DB_STATE:-unreachable}', not available." >&2
  echo "The rotation rewrites every audit row. The platform is stopped nightly by the" >&2
  echo "EventBridge schedules (deploy/aws/terraform/scheduler.tf) — run this inside the" >&2
  echo "maintenance window, after startup-sweep.sh has brought the database up." >&2
  exit 1
}

aws ecs describe-clusters --clusters "$CLUSTER" \
    --query 'clusters[0].clusterName' --output text 2>/dev/null | grep -qx "$CLUSTER" \
  || die "ECS cluster ${CLUSTER} not found."

aws ecs describe-task-definition --task-definition "$TASK_FAMILY" --output none 2>/dev/null \
  || die "task definition family ${TASK_FAMILY} not registered. Apply deploy/aws/terraform/rotation.tf first."

for svc in $ROLL; do
  aws ecs describe-services --cluster "$CLUSTER" --services "$svc" \
      --query 'services[0].serviceName' --output text 2>/dev/null | grep -qx "$svc" \
    || die "service ${svc} not found in cluster ${CLUSTER}."
done

[ -n "$SUBNETS" ]        || die "set ROTATE_SUBNETS (terraform output private_subnet_ids)."
[ -n "$SECURITY_GROUP" ] || die "set ROTATE_SECURITY_GROUP (terraform output task_security_group_id)."

read_secret_to_file || die "cannot read ${SECRET_ID}, or it is not JSON."
SECRET_FILE="$TMPFILE"
secret_has_key APP_KEY "$SECRET_FILE" || die "${SECRET_ID} has no APP_KEY."

STAGED=no
secret_has_key APP_KEY_NEXT "$SECRET_FILE" && STAGED=yes

# What each quiesced service was running BEFORE the rotation. Read from the
# live service rather than hardcoded: the desired count already lives in two
# places — local.services in main.tf, and the DESIRED table in
# startup-sweep.sh that reasserts it every morning and says so in block
# capitals. This script must not become a third. It restores what it found.
#
# --finish is the case that cannot read it, because by then the services
# are at zero and zero is not what to restore. So --apply persists the pair
# into the secret next to APP_KEY_NEXT, under a key nothing injects, and
# --finish reads it back. Operational state in a secret is not lovely, but
# it is what makes --finish need nothing from the machine that started the
# rotation, and it has exactly the same lifetime as APP_KEY_NEXT.
ORIGINAL=""
if [ "$MODE" = finish ]; then
  ORIGINAL="$(jq -r '.APP_KEY_ROTATION_RESTORE // ""' < "$SECRET_FILE")"
else
  for svc in $QUIESCE; do
    n="$(desired_count "$svc")"
    case "$n" in
      ''|*[!0-9]*) die "cannot read desiredCount for ${svc} (got '${n:-nothing}')." ;;
      0) die "${svc} is already at desired 0. Something else is mid-operation; resolve that first." ;;
    esac
    ORIGINAL="${ORIGINAL} ${svc}:${n}"
  done
fi

if [ "$MODE" = apply ] && [ "$STAGED" = yes ]; then
  echo "ABORT: ${SECRET_ID} already carries APP_KEY_NEXT, so a rotation is already in flight." >&2
  echo "If its rotation task succeeded, finish it:  $0 --finish" >&2
  echo "If it failed at the dump (nothing changed), delete the APP_KEY_NEXT key and start over." >&2
  echo "If it failed at the restore, DO NOT start over — restore the pre-rotation snapshot." >&2
  exit 1
fi
if [ "$MODE" = finish ]; then
  [ "$STAGED" = yes ] || die "--finish needs APP_KEY_NEXT in ${SECRET_ID}; there is nothing staged to promote."
  [ -n "$ORIGINAL" ] || say "WARNING: no APP_KEY_ROTATION_RESTORE in the secret. Services already at desired 0 will be rolled but NOT scaled back — check their counts against main.tf afterwards."
fi

if [ "$MODE" = dry ]; then
  cat >&2 <<PLAN
# dry run — nothing was changed. Re-run with --apply.
#
# Preflight passed: ${DB_INSTANCE} available, cluster ${CLUSTER} reachable,
# ${TASK_FAMILY} registered, ${SECRET_ID} holds APP_KEY, APP_KEY_NEXT staged: ${STAGED}.
#
# Stopped for the re-encryption, and restored to exactly these counts afterwards:
PLAN
  for e in $ORIGINAL; do echo "#   ${e%%:*}  desired ${e#*:} → 0 → ${e#*:}" >&2; done
  cat >&2 <<PLAN
# Restarted to pick up the new key:  ${ROLL}
#
# It will:
#   1. snapshot ${DB_INSTANCE} as ${SNAPSHOT_ID} and wait for it — the only
#      recovery path if the re-encryption fails halfway
#   2. mint a key locally and write it to ${SECRET_ID} as APP_KEY_NEXT
#   3. scale the writers to 0 and wait for runningCount 0
#   4. run ${TASK_FAMILY}: dump under APP_KEY, restore under APP_KEY_NEXT, shred
#   5. promote APP_KEY := APP_KEY_NEXT (APP_KEY_NEXT kept, for --finish and for step 7)
#   6. roll every Laravel service, restore the counts, wait for stable
#   7. verify a row decrypts under the promoted key, then drop the staging keys
#
# THE PLATFORM IS DOWN between 3 and 6. That is the trade this design makes,
# and rotate-app-key-inside.sh's header says why there is no alternative:
# with APP_MAINTENANCE_DRIVER at 'file' and two octane tasks, a maintenance
# page cannot be made to cover both.
PLAN
  exit 0
fi

# --------------------------------------------------- 1. the recovery asset ---
if [ "$MODE" = apply ]; then
  say "snapshotting ${DB_INSTANCE} as ${SNAPSHOT_ID} before anything changes..."
  aws rds create-db-snapshot --db-instance-identifier "$DB_INSTANCE" \
      --db-snapshot-identifier "$SNAPSHOT_ID" --output none 2>/dev/null \
    || die "could not start the pre-rotation snapshot. Refusing to re-encrypt without one."
  aws rds wait db-snapshot-available --db-snapshot-identifier "$SNAPSHOT_ID" 2>/dev/null \
    || die "snapshot ${SNAPSHOT_ID} did not become available. Refusing to re-encrypt without one."
  say "snapshot ${SNAPSHOT_ID} available."

  # ------------------------------------------------------- 2. mint + stage ---
  # 32 random bytes, base64, `base64:`-prefixed — the exact shape
  # `php artisan key:generate --show` emits for the default AES-256-CBC
  # cipher. Minted here rather than in the container so it never transits a
  # container's stdout, which on ECS is CloudWatch Logs: persisted, indexed
  # and readable by anyone with logs:FilterLogEvents. That is strictly
  # worse than the scrollback the Azure script was careful to keep it out of.
  NEWKEY="base64:$(openssl rand -base64 32)"
  case "$NEWKEY" in base64:?*) ;; *) die "minted key has an unexpected shape." ;; esac

  # Not through rewrite_secret: this is the one write that needs jq --arg,
  # so that the key is passed as data rather than interpolated into a filter.
  mktemp_secret || die "cannot create a temp file for the staged secret."
  STAGE_FILE="$TMPFILE"
  jq --arg k "$NEWKEY" --arg r "${ORIGINAL# }" \
     '.APP_KEY_NEXT = $k | .APP_KEY_ROTATION_RESTORE = $r' < "$SECRET_FILE" > "$STAGE_FILE" \
    || die "could not build the staged secret."
  unset NEWKEY
  write_secret_from_file "$STAGE_FILE" || die "could not write APP_KEY_NEXT to ${SECRET_ID}."
  say "APP_KEY_NEXT staged. Nothing else has changed; the old key still reads every row."

  # ------------------------------------------------------------ 3. quiesce ---
  # Up to here every failure is fully reversible, and is reversed rather
  # than left for a human to remember.
  abandon() {
    say "$1 Rolling back."
    restore_counts "$ORIGINAL"
    mktemp_secret && jq 'del(.APP_KEY_NEXT, .APP_KEY_ROTATION_RESTORE)' < "$STAGE_FILE" > "$TMPFILE" \
      && write_secret_from_file "$TMPFILE" \
      && say "staging keys removed; the secret is exactly as it was."
    echo "APP_KEY rotation ABANDONED. Nothing was re-encrypted." >&2
    echo "Delete the unused snapshot when you are done with it:" >&2
    echo "  aws rds delete-db-snapshot --db-snapshot-identifier ${SNAPSHOT_ID}" >&2
    exit 1
  }

  say "scaling the audit-log writers to 0: ${QUIESCE}"
  for svc in $QUIESCE; do
    aws ecs update-service --cluster "$CLUSTER" --service "$svc" \
        --desired-count 0 --output none 2>/dev/null \
      || abandon "could not scale ${svc} to 0."
  done
  for svc in $QUIESCE; do
    if ! left="$(wait_running "$svc" 0)"; then
      abandon "${svc} still reports ${left} running tasks; a writer that is still up would encrypt rows under the old key mid-rotation."
    fi
    say "${svc}: 0 running."
  done

  # ------------------------------------------------- 4. the rotation task ---
  say "running ${TASK_FAMILY} (dump under APP_KEY → restore under APP_KEY_NEXT → shred)..."
  run_rotation_task "$INSIDE"; RT=$?; EXIT_CODE="$LAST_TASK_EXIT"

  if [ "$RT" = 1 ]; then
    # The same distinction CD's smoke gate draws: "it failed" and "it could
    # not run" are different facts. Nothing was re-encrypted.
    abandon "run-task returned no task ARN — the rotation did not start."
  fi
  if [ "$RT" = 2 ]; then
    cat >&2 <<PENDING
ABORT: the rotation task did not stop within the waiter's budget.
It may STILL BE re-encrypting. Do not start another one, and do not scale
anything up until you know what it did.
  aws ecs describe-tasks --cluster ${CLUSTER} --tasks ${LAST_TASK_ARN}
  logs: the services log group, stream prefix app-key-rotation
  snapshot: ${SNAPSHOT_ID}
PENDING
    exit 1
  fi
  say "rotation task ${LAST_TASK_ARN} exit ${EXIT_CODE} (${LAST_TASK_REASON})"

  case "$EXIT_CODE" in
    0) ;;
    30)
      say "WARNING: the ledger IS re-encrypted, but the task could not shred its plaintext dump. Fargate destroys that disk with the task, so this is hygiene rather than exposure. Continuing."
      ;;
    10|64)
      # The dump failed, or the task refused before touching anything.
      abandon "the rotation stopped before re-encrypting anything (exit ${EXIT_CODE})."
      ;;
    *)
      # Exit 20, or a task killed during the restore. The ledger may be half
      # re-encrypted, which is the one state no key reads and no re-dump can
      # recover. Traffic must not come back, and the secret must NOT be
      # rolled back: the old key is right for some rows and wrong for others
      # either way.
      cat >&2 <<STUCK
ABORT: the rotation task exited ${EXIT_CODE} (${LAST_TASK_REASON}).
query_audit_log may be HALF re-encrypted. No key reads it in that state, and
audit:dump-pii cannot re-dump it — DumpAuditPii.php:186 fails the whole dump
on the first row it cannot decrypt.

DO NOT scale ${QUIESCE} back up: a half-rotated ledger must not serve traffic.
DO NOT remove APP_KEY_NEXT, and do not put the old key anywhere.

Recovery is the snapshot this run took first:
  ${SNAPSHOT_ID}
Restore it, leave APP_KEY as it is (still the old key — nothing was promoted),
then bring the services back. ops/runbooks/secret-rotation.md §2 has the steps.

Read the task's log before any retry, so the next attempt is not the same one:
  aws ecs describe-tasks --cluster ${CLUSTER} --tasks ${LAST_TASK_ARN}
  logs: the services log group, stream prefix app-key-rotation
STUCK
      exit 1
      ;;
  esac
  SECRET_FILE="$STAGE_FILE"
fi

# ------------------------------------------------------------ 5. promote ---
# APP_KEY_NEXT is deliberately KEPT by this write rather than dropped in the
# same one. It is what makes --finish need nothing from this machine, and
# what lets step 7 verify on the rotation task definition — which cannot
# start without it.
say "promoting APP_KEY := APP_KEY_NEXT..."
mktemp_secret || die "cannot create a temp file for the promotion."
PROMOTE_FILE="$TMPFILE"
jq '.APP_KEY = .APP_KEY_NEXT' < "$SECRET_FILE" > "$PROMOTE_FILE" \
  || die "could not build the promoted secret."
write_secret_from_file "$PROMOTE_FILE" || {
  echo "ABORT: the ledger is re-encrypted but APP_KEY could not be promoted." >&2
  echo "The data is under APP_KEY_NEXT and the apps still hold the old key." >&2
  echo "Retry the promote-and-roll half:  $0 --finish" >&2
  exit 1
}
say "APP_KEY promoted. Every audit row is now readable only with this value."

# --------------------------------------------------- 6. roll and restore ---
# One failure here does not stop the others: a service left on the old key
# cannot read the ledger, so stopping early leaves MORE of them broken.
FAILURES=0
for svc in $ROLL; do
  want=""
  for e in $ORIGINAL; do [ "${e%%:*}" = "$svc" ] && want="${e#*:}"; done
  if [ -n "$want" ]; then
    aws ecs update-service --cluster "$CLUSTER" --service "$svc" \
        --desired-count "$want" --force-new-deployment --output none 2>/dev/null \
      || { say "FAILED: could not restore and roll ${svc} (wanted desired ${want})"; FAILURES=$((FAILURES + 1)); continue; }
  else
    aws ecs update-service --cluster "$CLUSTER" --service "$svc" \
        --force-new-deployment --output none 2>/dev/null \
      || { say "FAILED: could not roll ${svc}"; FAILURES=$((FAILURES + 1)); continue; }
  fi
  say "${svc}: rolling${want:+, back to desired ${want}}"
done

for svc in $ROLL; do
  if aws ecs wait services-stable --cluster "$CLUSTER" --services "$svc" 2>/dev/null; then
    say "${svc}: stable on the new key."
  else
    say "FAILED: ${svc} did not reach a steady state. Note that services.tf's deployment circuit breaker rolls back to the previous TASK DEFINITION, which cannot help here — the key comes from Secrets Manager at task start, not from the task definition. Read its logs."
    FAILURES=$((FAILURES + 1))
  fi
done

# ------------------------------------------------------------- 7. verify ---
# Proves the promoted key decrypts the re-encrypted rows, rather than
# assuming the two halves agree. Runs on the rotation task definition
# because that is the one place both keys are still injected — which is
# why the staging keys are not dropped until after this.
mktemp_secret || die "cannot create a temp file for the verification."
VERIFY_SRC="$TMPFILE"
cat > "$VERIFY_SRC" <<'VERIFY'
php artisan tinker --execute '
  $r = App\Models\QueryAuditLog::query()->latest("audit_id")->first();
  if ($r === null) { echo "VERIFY_NO_ROWS"; exit(0); }
  if ($r->query_text === null) { echo "VERIFY_NULL"; exit(1); }
  echo "VERIFY_DECRYPT_OK";
'
VERIFY
run_rotation_task "$VERIFY_SRC"; VT=$?; VRC="$LAST_TASK_EXIT"
if [ "$VT" = 0 ] && [ "$VRC" = "0" ]; then
  say "verify: a fresh task decrypted an audit row under the promoted key (task ${LAST_TASK_ARN})."
else
  # Not a failure of the rotation — the data and the secret are consistent
  # whether or not this ran. But an unverified rotation is an unverified
  # rotation, and the probe's lesson is that a gate which cannot run must
  # not report success.
  say "WARNING: could not verify a decrypt through a fresh task (rc=${VT}, container exit=${VRC:-none}). The rotation itself completed; check a real query before calling this done."
  FAILURES=$((FAILURES + 1))
fi

# ------------------------------------------------------------ 8. clean up ---
if [ "$FAILURES" -eq 0 ]; then
  if rewrite_secret 'del(.APP_KEY_NEXT, .APP_KEY_ROTATION_RESTORE)' "$PROMOTE_FILE"; then
    say "staging keys removed. ${TASK_FAMILY} is unstartable again until the next rotation."
  else
    say "WARNING: the staging keys could not be removed from ${SECRET_ID}. Harmless — nothing injects APP_KEY_ROTATION_RESTORE and nothing reads APP_KEY_NEXT — but remove them before the next rotation, which refuses to start while APP_KEY_NEXT is there."
  fi
  cat >&2 <<DONE
APP_KEY rotated. Services on the new key: ${ROLL}. Decrypt verified.
Next:
  - log the rotation to authz_audit (ops/runbooks/secret-rotation.md §16)
  - keep snapshot ${SNAPSHOT_ID} until you are satisfied, then delete it:
      aws rds delete-db-snapshot --db-snapshot-identifier ${SNAPSHOT_ID}
  - the previous secret version is AWSPREVIOUS in ${SECRET_ID}. The OLD key
    is in it, and it now reads NOTHING. Do not put it back.
DONE
  exit 0
fi

cat >&2 <<INCOMPLETE
APP_KEY rotation INCOMPLETE: ${FAILURES} step(s) failed AFTER the data was
re-encrypted and APP_KEY promoted. The ledger is consistent; some service may
not be running the new key yet.

Re-run the promote-and-roll half. It is idempotent and needs nothing from this
machine, because APP_KEY_NEXT is still in ${SECRET_ID}:
  $0 --finish

Do NOT put the old key back: every row has been under the new one since the
rotation task exited 0. Snapshot ${SNAPSHOT_ID} is still there if you need it.
INCOMPLETE
exit 1
