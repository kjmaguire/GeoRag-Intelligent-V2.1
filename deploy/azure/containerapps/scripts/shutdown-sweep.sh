#!/usr/bin/env bash
# Nightly cost-control shutdown sweep (plan C6) for the `georag` resource
# group. Runs as the inline body of the shutdown-scheduler-cc Container
# Apps Job; see deploy/azure/containerapps/shutdown-job.yaml, which embeds
# this file verbatim. scripts/check_scheduler_job_parity.py fails CI if the
# two copies drift, so edit THIS file and re-run the parity check.
#
# Read shutdown-job.yaml's header for what the sweep actually achieves
# (fewer apps stop than you would think) and for the replicaTimeout
# history. This header covers only the script's own contract.
#
# ---------------------------------------------------------------------
# WHY THIS IS NOT `set -e` AND NOT `|| echo "skip ..."`
# ---------------------------------------------------------------------
# The original body ran `set -euo pipefail` and then masked every command
# with `|| echo "skip $app"`. That combination is the worst of both: the
# mask defeats -e, so a failed action never stops the sweep, and it never
# reaches the exit status either, so the job execution reports Succeeded
# with nothing amiss in the log except the word "skip".
#
# Removing the masks is not the fix. Under `set -e` a single failed
# `az containerapp update` would abort the sweep and leave the remaining
# apps at min-replicas 1 — and on the startup side it would leave the
# whole platform down for the working day. The eight actions are
# independent; one failing is not a reason to skip the other seven.
#
# So: no -e, every action attempted, failures collected, and a non-zero
# exit at the end. The job execution status then means what it says, and
# the alert rule in deploy/azure/alerts/ has something true to watch.
#
# ---------------------------------------------------------------------
# WHY PROGRESS GOES TO STDERR
# ---------------------------------------------------------------------
# bash's stdout is a pipe here, so it is block-buffered and only flushes
# when the process exits. Measured in ContainerAppConsoleLogs_CL on the
# 2026-08-20 run: every stderr line (the az CLI's own progress, the
# `WARNING: Server will be automatically started after 7 days` at
# 00:02:22) appears at its real time, while `skip postgres` and
# `shutdown sweep complete` — both stdout — carry time_t 00:05:00.5395
# and 00:05:00.5396, i.e. the instant the container was terminated, two
# and a half minutes after the events they describe.
#
# That is also how a truncated sweep managed to print "shutdown sweep
# complete": the line was already in the buffer, and the flush on
# teardown made a killed run look finished. Anything this script says
# about its own progress goes to stderr so it is emitted when it happens
# and survives the container being killed mid-sweep.
#
# ---------------------------------------------------------------------
# WHY THE POSTGRES RESULT IS CHECKED BY STATE, NOT BY EXIT CODE
# ---------------------------------------------------------------------
# `az postgres flexible-server stop` returns non-zero both when the stop
# genuinely failed and when the server was already stopped, and the CLI's
# long-running-operation poll can also be interrupted after ARM has
# already accepted the operation. Matching on the error string means
# guessing Azure's error codes. Reading the server's state afterwards
# does not: if the server is Stopped or Stopping, the sweep got what it
# came for, whatever the command returned. The converse matters more —
# an exit code of 0 with the server still Ready is a failure, and only a
# state read catches it.
set -uo pipefail

RG="${SWEEP_RESOURCE_GROUP:-georag}"
PG_SERVER="${SWEEP_PG_SERVER:-georag-pg-cc}"

# Apps the sweep drops to min-replicas 0. Order is irrelevant — nothing
# here depends on anything else, unlike the startup tiers.
APPS=(
  redis-cc
  qdrant-cc
  hatchet-worker-cc
  hatchet-cc
  fastapi-cc
  laravel-octane-cc
  laravel-horizon-cc
  laravel-reverb-cc
)

FAILURES=()

log()  { printf '%s\n' "$*" >&2; }
fail() { FAILURES+=("$1"); log "FAILED: $1"; }

# --- DST guard --------------------------------------------------------
# Container Apps Jobs schedule in UTC only, so the job fires at both
# candidate hours and this drops whichever one is not 23:00 US-Pacific.
# That keeps the local time correct across DST with no biannual edit.
#
# The transition instants are computed as epochs rather than parsed as
# dates: US DST starts on the second Sunday of March at 10:00 UTC (02:00
# PST) and ends on the first Sunday of November at 09:00 UTC (02:00 PDT).
# The earlier version compared against 00:00 UTC on those Sundays, which
# put the offset an hour wrong for the 00:00-10:00 UTC window on the two
# changeover days — never a double fire or a missed fire, but the sweep
# ran an hour early each March and an hour late each November.
#
# SWEEP_NOW_EPOCH exists for the test harness
# (deploy/azure/containerapps/scripts/tests/run.sh), which has to be able
# to stand on both sides of a transition. Unset in production.
TARGET_LOCAL_HOUR="${SWEEP_TARGET_LOCAL_HOUR:-23}"
NOW="${SWEEP_NOW_EPOCH:-$(date -u +%s)}"
YEAR=$(date -u -d "@${NOW}" +%Y)

first_sunday_epoch() {
  # $1 = YYYY-MM-01. %u gives 1=Monday .. 7=Sunday.
  local weekday add
  weekday=$(date -u -d "$1" +%u)
  add=$(( (7 - weekday) % 7 ))
  date -u -d "$1 +${add} days" +%s
}

DST_START=$(( $(first_sunday_epoch "${YEAR}-03-01") + 7 * 86400 + 10 * 3600 ))
DST_END=$((   $(first_sunday_epoch "${YEAR}-11-01")               + 9 * 3600 ))

if [ "$NOW" -ge "$DST_START" ] && [ "$NOW" -lt "$DST_END" ]; then
  OFFSET_HOURS=-7   # PDT
else
  OFFSET_HOURS=-8   # PST
fi

LOCAL_HOUR=$(date -u -d "@$(( NOW + OFFSET_HOURS * 3600 ))" +%H)
if [ "$LOCAL_HOUR" != "$TARGET_LOCAL_HOUR" ]; then
  log "DST-safety double-fire: US-Pacific local hour is ${LOCAL_HOUR} (offset ${OFFSET_HOURS}h), not ${TARGET_LOCAL_HOUR} -- skipping"
  exit 0
fi

# --- sweep ------------------------------------------------------------
if ! az login --identity --output none; then
  log "FATAL: az login --identity failed; no action taken"
  exit 1
fi

log "--- dropping ${#APPS[@]} apps to min-replicas 0 ---"
for app in "${APPS[@]}"; do
  # --output none: each `az containerapp update` otherwise dumps the app's
  # entire JSON, env var list included. The two scheduler jobs emitted
  # 13,197 console lines over 2026-08-20..21 that way, which is what
  # buries the handful of lines that matter.
  if az containerapp update -g "$RG" -n "$app" --min-replicas 0 --output none; then
    log "$app: min-replicas 0"
  else
    fail "min-replicas 0 on $app"
  fi
done

log "--- stopping ${PG_SERVER} ---"
stop_rc=0
az postgres flexible-server stop -g "$RG" -n "$PG_SERVER" --output none || stop_rc=$?

pg_state=$(az postgres flexible-server show -g "$RG" -n "$PG_SERVER" \
             --query state -o tsv 2>/dev/null || echo "")
case "$pg_state" in
  Stopped|Stopping|Disabled)
    if [ "$stop_rc" -ne 0 ]; then
      log "${PG_SERVER}: stop command exited ${stop_rc} but server is ${pg_state} -- treating as success"
    else
      log "${PG_SERVER}: ${pg_state}"
    fi
    ;;
  "")
    fail "could not read ${PG_SERVER} state after stop (stop exited ${stop_rc})"
    ;;
  *)
    fail "${PG_SERVER} is still ${pg_state} after stop (stop exited ${stop_rc})"
    ;;
esac

TOTAL=$(( ${#APPS[@]} + 1 ))
if [ ${#FAILURES[@]} -eq 0 ]; then
  log "shutdown sweep complete: ${TOTAL}/${TOTAL} actions succeeded"
  exit 0
fi

log "shutdown sweep INCOMPLETE: ${#FAILURES[@]} of ${TOTAL} actions failed"
for f in "${FAILURES[@]}"; do
  log "  - $f"
done
exit 1
