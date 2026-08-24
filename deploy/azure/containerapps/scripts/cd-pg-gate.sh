#!/usr/bin/env bash
# Deploy-time Postgres gate for .github/workflows/cd.yml (which invokes
# this file from the repo checkout — unlike the sweeps it is NOT embedded
# in any Container Apps Job YAML, so check_scheduler_job_parity.py does
# not apply here, and the caller is already authenticated: no `az login`).
#
# The deploy job's migrate step needs georag-pg-cc up. The server is
# deliberately Stopped for part of every night — the shutdown sweep stops
# it and scales the HTTP tier to zero at 23:00 US-Pacific, and the
# startup sweep restores everything at 06:00 US-Pacific (see
# shutdown-job.yaml / startup-job.yaml; in UTC that is 06:00→13:00 in PDT
# and 07:00→14:00 in PST). A merge to main inside that window used to
# sail through CD's gates and die at the migrate step with a generic
# "migration job ended in Failed" (run 32701044183, 2026-08-24 07:41 UTC)
# that said nothing about why.
#
# ---------------------------------------------------------------------
# WHY THIS GATE FAILS FAST INSTEAD OF STARTING THE SERVER
# ---------------------------------------------------------------------
# Starting Postgres is not enough to deploy: the shutdown sweep also
# leaves fastapi-cc and the three laravel apps at min-replicas 0, and
# CD's post-deploy smoke execs into fastapi-cc — an app with zero
# replicas has nothing to exec into, so an in-window deploy ends in
# "smoke inconclusive" even with the database up. Making the deploy
# genuinely work mid-window means re-implementing the startup AND
# shutdown sweeps inside cd.yml (plus deciding who re-stops a server a
# cancelled run started, and what happens to the Hatchet crons scheduled
# inside the window). That machinery already exists, tested, in the two
# sweep scripts — CD should not grow a private copy of it. So: report
# the true state and stop, before anything has been mutated. The
# rollback step's "nothing was deployed; nothing to roll back" is then
# exactly accurate.
#
# Judged by state, not exit code, per shutdown-sweep.sh's header. The
# one state worth waiting on is Starting/Updating: an operator
# restarting the stack inside the window is a daily occurrence
# (ServerIsNotStopped, 15 of 15 retained days — see startup-sweep.sh),
# and a deploy that lands mid-restart should ride it out, not fail.
#
# The 23:00–06:00 US-Pacific window below is ADVISORY: both branches of
# the Stopped path exit 1, the window only picks which explanation to
# print. If the sweeps' target hours ever change, the worst a stale copy
# here produces is a misleading hint, never a wrong action. The DST
# arithmetic is the sweeps' own (see shutdown-sweep.sh for why the
# transition instants are 10:00/09:00 UTC).
#
# Env (all optional):
#   GATE_RESOURCE_GROUP  resource group                     (georag)
#   GATE_PG_SERVER       flexible server name               (georag-pg-cc)
#   GATE_WAIT_TIMEOUT    seconds to wait out Starting       (600)
#   GATE_WAIT_INTERVAL   poll interval seconds              (10)
#   GATE_RUN_ID          workflow run id for the rerun hint ("")
#   GATE_NOW_EPOCH       clock override for the test harness
set -uo pipefail

RG="${GATE_RESOURCE_GROUP:-georag}"
PG_SERVER="${GATE_PG_SERVER:-georag-pg-cc}"
WAIT_TIMEOUT="${GATE_WAIT_TIMEOUT:-600}"
WAIT_INTERVAL="${GATE_WAIT_INTERVAL:-10}"
RUN_ID="${GATE_RUN_ID:-}"

log() { printf '%s\n' "$*" >&2; }

pg_state() {
  az postgres flexible-server show -g "$RG" -n "$PG_SERVER" \
    --query state -o tsv 2>/dev/null || echo ""
}

# --- advisory window classification ----------------------------------
# Same epoch arithmetic as the sweeps' DST guard: US DST starts on the
# second Sunday of March at 10:00 UTC and ends on the first Sunday of
# November at 09:00 UTC.
NOW="${GATE_NOW_EPOCH:-$(date -u +%s)}"
YEAR=$(date -u -d "@${NOW}" +%Y)

first_sunday_epoch() {
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

LOCAL_EPOCH=$(( NOW + OFFSET_HOURS * 3600 ))
LOCAL_HOUR=$((10#$(date -u -d "@${LOCAL_EPOCH}" +%H)))
LOCAL_MIN=$((10#$(date -u -d "@${LOCAL_EPOCH}" +%M)))
# 06:00-06:30 local still counts as the window: the startup sweep
# budgets up to ~20 minutes of tiered waits after its 06:00 firing
# (deploy/azure/alerts/create-alerts.sh pads its suppression to :30 for
# the same reason), so a server still Stopped at 06:05 is the sweep
# mid-run, not an incident.
IN_WINDOW=no
if [ "$LOCAL_HOUR" -ge 23 ] || [ "$LOCAL_HOUR" -lt 6 ] \
   || { [ "$LOCAL_HOUR" -eq 6 ] && [ "$LOCAL_MIN" -lt 30 ]; }; then
  IN_WINDOW=yes
fi

# --- gate -------------------------------------------------------------
# One ARM 429/503 blip must not fail a deploy with a wrong diagnosis, so
# the initial read gets three attempts before "unreadable" is believed.
STATE=$(pg_state)
if [ -z "$STATE" ]; then
  for _ in 1 2; do
    sleep "$WAIT_INTERVAL"
    STATE=$(pg_state)
    [ -n "$STATE" ] && break
  done
fi
log "${PG_SERVER} state: ${STATE:-<unreadable>} (US-Pacific hour ${LOCAL_HOUR}, nightly window: ${IN_WINDOW})"

case "$STATE" in
  Ready)
    log "${PG_SERVER} is Ready -- proceeding"
    exit 0
    ;;

  Starting|Updating)
    # An operator restart or maintenance op in flight; ride it out.
    log "${PG_SERVER} is ${STATE} -- waiting up to ${WAIT_TIMEOUT}s for Ready"
    waited=0
    while [ "$waited" -lt "$WAIT_TIMEOUT" ]; do
      sleep "$WAIT_INTERVAL"
      waited=$(( waited + WAIT_INTERVAL ))
      STATE=$(pg_state)
      log "  ${STATE:-<unreadable>} (${waited}s)"
      if [ "$STATE" = "Ready" ]; then
        log "${PG_SERVER} is Ready -- proceeding"
        exit 0
      fi
    done
    echo "::error::${PG_SERVER} did not reach Ready within ${WAIT_TIMEOUT}s (last state: ${STATE:-unreadable}) -- re-run this workflow once it is Ready"
    exit 1
    ;;

  Stopped|Stopping|Disabled)
    # Disabled is a normal post-stop state (shutdown-sweep.sh accepts
    # Stopped|Stopping|Disabled as "the sweep got what it came for"), so
    # it gets the same window-vs-incident story, not the generic arm.
    if [ "$IN_WINDOW" = "yes" ]; then
      echo "::error::${PG_SERVER} is ${STATE}: the platform is inside its nightly cost window (23:00-06:00 US-Pacific). Nothing is wrong -- re-run this deploy after the startup sweep has finished (it fires at 06:00 US-Pacific = 13:00 UTC in PDT, 14:00 UTC in PST, and budgets ~20 min of waits)${RUN_ID:+:  gh run rerun ${RUN_ID}}"
      log "The shutdown sweep also scales the HTTP tier to zero, so deploying"
      log "mid-window cannot pass the post-deploy smoke even with the database"
      log "up -- which is why this gate stops here instead of starting the"
      log "server. Nothing has been touched; there is nothing to roll back."
    else
      echo "::error::${PG_SERVER} is ${STATE} OUTSIDE the nightly cost window -- a real incident, not the scheduler. See ops/runbooks/azure-oncall.md ('Postgres is stopped when it should be running') and check the shutdown-scheduler-cc execution list before re-running."
    fi
    exit 1
    ;;

  "")
    echo "::error::could not read ${PG_SERVER} state -- refusing to deploy blind. Check 'az postgres flexible-server show -g ${RG} -n ${PG_SERVER}' by hand."
    exit 1
    ;;

  *)
    echo "::error::${PG_SERVER} is in state '${STATE}' -- cannot deploy against it. See ops/runbooks/azure-oncall.md."
    exit 1
    ;;
esac
