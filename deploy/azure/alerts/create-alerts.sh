#!/usr/bin/env bash
# Idempotent creation of the Azure Monitor rules the `georag` group is
# missing. Prints the commands by default; pass --apply to run them.
#
#   bash deploy/azure/alerts/create-alerts.sh            # show
#   bash deploy/azure/alerts/create-alerts.sh --apply    # create/update
#
# ON WINDOWS, run it as:
#
#   MSYS_NO_PATHCONV=1 bash deploy/azure/alerts/create-alerts.sh --apply
#
# Git Bash rewrites any argument that looks like a Unix absolute path, and
# every --scopes / --resource / --action here is an Azure resource ID
# starting with /subscriptions/. Without that variable they arrive as
# C:/Program Files/Git/subscriptions/... and Azure rejects the first
# command with LinkedInvalidPropertyId. Native bash needs nothing.
#
# ---------------------------------------------------------------------
# WHAT IS ALREADY THERE, SO THIS DOES NOT DUPLICATE IT
# ---------------------------------------------------------------------
# Measured 2026-08-21: 15 metric alerts (8 per-app restart counters, 3 on
# georag-pg-cc, 2 each on fastapi-cc and hatchet-worker-cc), 4 scheduled
# query rules (qdrant-cc-optimizer-stuck, georag-ingest-failed,
# georag-fastapi-critical, georag-worker-exception-spike), one alert
# PROCESSING rule (georag-pg-shutdown-window), and ZERO activity-log
# alerts. All route to georag-alerts-ag, whose only receiver is
# kylejmaguire@gmail.com.
#
# The gaps below are the ones where Azure is already collecting the data
# and nothing is looking at it -- plus one repair: the existing
# processing rule hard-codes a maintenance window that moved on
# 2026-08-21 and is now wrong in both directions. See section 4c.
#
# ---------------------------------------------------------------------
# THE COLUMN NOBODY WOULD GUESS
# ---------------------------------------------------------------------
# Container App JOBS do not populate ContainerAppName_s. Their console
# logs land in ContainerAppConsoleLogs_CL with that column EMPTY and the
# job name in ContainerJobName_s. A rule written the obvious way --
# `where ContainerAppName_s == 'shutdown-scheduler-cc'` -- parses, runs,
# costs money and matches nothing, forever, silently. Every query here
# was executed against the live workspace before being written down.
set -euo pipefail

RG=georag
WS_NAME=workspace-georag4ad7
SUB=d314ab40-b5b7-4e3e-8308-86023fb7638a
ACTION_GROUP="/subscriptions/${SUB}/resourceGroups/${RG}/providers/microsoft.insights/actionGroups/georag-alerts-ag"
WS_ID="/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.OperationalInsights/workspaces/${WS_NAME}"
CA_ID="/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.App/containerApps"
FOUNDRY_ID="/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.CognitiveServices/accounts/georag-foundry-cc"
PG_ID="/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.DBforPostgreSQL/flexibleServers/georag-pg-cc"
LOCATION="${ALERT_LOCATION:-canadacentral}"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
JOBS_DIR="${REPO_ROOT}/deploy/azure/containerapps"

# Path + line-ending handling for a Windows az called from Git Bash or WSL.
. "${REPO_ROOT}/deploy/azure/_host_compat.sh"

run() {
  if [ "$APPLY" -eq 1 ]; then
    "$@"
  else
    printf '%q ' "$@"; printf '\n\n'
  fi
}

# --- the maintenance window, derived not restated ---------------------
# The dead-air rule below has to be suppressed while the platform is
# deliberately down, or it pages every single night -- which is the
# alert-fatigue trap that already made georag-pg-cc-down worthless (it is
# a Sev0 that fires by design every night). The window is defined by the
# schedulers' crons, so it is read from them rather than written here a
# second time: change the crons and this follows.
cron_hours() {
  grep -oE 'cronExpression: "0 [0-9,]+' "$1" | grep -oE '[0-9,]+$'
}
SHUTDOWN_HOURS=$(cron_hours "${JOBS_DIR}/shutdown-job.yaml")
STARTUP_HOURS=$(cron_hours "${JOBS_DIR}/startup-job.yaml")
WINDOW_START=$(printf '%s' "$SHUTDOWN_HOURS" | tr ',' '\n' | sort -n | head -1)
WINDOW_LAST_START=$(printf '%s' "$STARTUP_HOURS" | tr ',' '\n' | sort -n | tail -1)
# The suppression ends 30 minutes past the LATEST candidate startup hour
# (the one the guard uses in standard time). The tiered sweep budgets up
# to 20 minutes of waits before the last app is up, so 30 covers it.
SUPPRESS_FROM=$(printf '%02d:00:00' "$WINDOW_START")
SUPPRESS_TO=$(printf '%02d:30:00' "$WINDOW_LAST_START")

echo "# maintenance window derived from the job crons: ${SUPPRESS_FROM}-${SUPPRESS_TO} UTC" >&2
echo >&2

# --- 1. the schedulers report a failed sweep --------------------------
# Only meaningful once the honest sweep scripts are applied: the current
# production body exits 0 and prints "sweep complete" no matter what
# happened, so there is nothing for a rule to key on. See
# deploy/azure/containerapps/scripts/.
run az monitor scheduled-query create \
  -g "$RG" -n scheduler-sweep-failed \
  --scopes "$WS_ID" \
  --location "$LOCATION" \
  --description "A nightly sweep reported at least one failed action, or could not authenticate." \
  --severity 1 \
  --evaluation-frequency 1h --window-size 1h \
  --condition "count 'failures' > 0" \
  --condition-query failures="ContainerAppConsoleLogs_CL
| where ContainerJobName_s in ('shutdown-scheduler-cc','startup-scheduler-cc')
| where Log_s startswith 'shutdown sweep INCOMPLETE'
     or Log_s startswith 'startup sweep INCOMPLETE'
     or Log_s startswith 'FATAL:'" \
  --action-groups "$ACTION_GROUP"

# --- 2. dead-man switch: a sweep that never reported at all -----------
# The failure mode the exit status cannot cover. On 2026-08-19 and 08-20
# the shutdown container was killed at the 300s replicaTimeout mid-stop;
# a killed container emits no verdict line, so rule 1 stays quiet. 25h so
# a single missed night trips it without the daily boundary racing.
run az monitor scheduled-query create \
  -g "$RG" -n scheduler-sweep-missing \
  --scopes "$WS_ID" \
  --location "$LOCATION" \
  --description "A nightly sweep did not report completion in the last 25 hours." \
  --severity 1 \
  --evaluation-frequency 1h --window-size 1d \
  --condition "count 'missing' > 0" \
  --condition-query missing="let expected = datatable(job:string)['shutdown-scheduler-cc','startup-scheduler-cc'];
let completed = ContainerAppConsoleLogs_CL
  | where TimeGenerated > ago(25h)
  | where Log_s has 'sweep complete'
  | distinct ContainerJobName_s;
expected | where job !in (completed)" \
  --action-groups "$ACTION_GROUP"

# --- 3. the public endpoint is returning 5xx --------------------------
# laravel-octane-cc is the only externally reachable ingress and nothing
# watches whether it serves. Container Apps already emits Requests split
# by statusCodeCategory at no cost. Measured over 2026-08-20..21: 5,829
# 2xx across 47 of 48 hours, 20 4xx, 13 3xx, and 10 5xx -- all ten inside
# a single hour. A burst, not a trickle, so a low threshold on a short
# window is the right shape. Tune with that measurement in hand.
run az monitor metrics alert create \
  -g "$RG" -n laravel-octane-cc-5xx \
  --scopes "${CA_ID}/laravel-octane-cc" \
  --description "laravel-octane-cc is returning server errors." \
  --severity 2 \
  --evaluation-frequency 5m --window-size 15m \
  --condition "total Requests > 5 where statusCodeCategory includes 5xx" \
  --action "$ACTION_GROUP"

# --- 4. the public endpoint is serving nothing ------------------------
# The crash-loop signature the restart counter cannot distinguish from a
# single healthy restart. Baseline over the same 48h: exactly one hour at
# zero requests, and that hour was inside the maintenance window -- which
# is why rule 4b exists.
run az monitor metrics alert create \
  -g "$RG" -n laravel-octane-cc-dead-air \
  --scopes "${CA_ID}/laravel-octane-cc" \
  --description "laravel-octane-cc served no requests for 30 minutes outside the maintenance window." \
  --severity 1 \
  --evaluation-frequency 5m --window-size 30m \
  --condition "total Requests <= 0" \
  --action "$ACTION_GROUP"

# --- 4b. ...but not while the platform is deliberately down -----------
run az monitor alert-processing-rule create \
  -g "$RG" -n suppress-during-maintenance \
  --rule-type RemoveAllActionGroups \
  --scopes "${CA_ID}/laravel-octane-cc" \
  --filter-alert-rule-name Equals laravel-octane-cc-dead-air \
  --description "The nightly window stops the HTTP tier on purpose; dead air is the intended state." \
  --schedule-recurrence-type Daily \
  --schedule-recurrence-start-time "$SUPPRESS_FROM" \
  --schedule-recurrence-end-time "$SUPPRESS_TO" \
  --schedule-time-zone UTC

# --- 4c. the SAME window, applied to the rule that already existed ----
# georag-pg-shutdown-window was created 2026-08-20 against the US-Eastern
# schedule and hard-codes 00:00:00-10:15:00 UTC. The window moved to
# US-Pacific on 2026-08-21 (crons 0 6,7 and 0 13,14). Its own description
# says "Widen or delete this rule the day the shutdown schedule changes -
# otherwise a real outage between 00:00 and 10:15 UTC goes unannounced."
# That day was yesterday, and leaving it alone breaks in BOTH directions:
#
#   10:15-14:00 UTC  Postgres is now deliberately stopped and no longer
#                    suppressed -- the only Sev0 on the subscription pages
#                    every morning, which is the exact alert-fatigue trap
#                    the rule was created to end.
#   00:00-06:00 UTC  Postgres is now UP and still suppressed -- a real
#                    overnight outage goes out with its action groups
#                    stripped, silently.
#
# Re-created here (create upserts by name) from the same two derived
# variables as rule 4b, so the next schedule change carries both rules
# with it instead of leaving this one behind again.
run az monitor alert-processing-rule create \
  -g "$RG" -n georag-pg-shutdown-window \
  --rule-type RemoveAllActionGroups \
  --scopes "/subscriptions/${SUB}/resourceGroups/${RG}" \
  --filter-alert-rule-name Contains georag-pg-cc-down \
  --description "Suppresses georag-pg-cc-down during the deliberate nightly shutdown. The window is DERIVED from the scheduler crons in deploy/azure/containerapps/ by deploy/azure/alerts/create-alerts.sh -- re-run that script with --apply after changing a cron rather than editing the times here." \
  --schedule-recurrence-type Daily \
  --schedule-recurrence-start-time "$SUPPRESS_FROM" \
  --schedule-recurrence-end-time "$SUPPRESS_TO" \
  --schedule-time-zone UTC

# --- 5. Foundry is refusing calls -------------------------------------
# Cognitive Services accounts emit token and error counts as FREE platform
# metrics -- no diagnostic setting needed, and real data is already there
# (1.87M TotalTokens on 2026-08-18). The answer path has no alerting at
# all today, and Foundry blocked 1,421 of 2,524 calls on 2026-08-17
# unnoticed.
run az monitor metrics alert create \
  -g "$RG" -n georag-foundry-cc-client-errors \
  --scopes "$FOUNDRY_ID" \
  --description "Azure AI Foundry is rejecting a significant share of calls (throttling, quota, or auth)." \
  --severity 2 \
  --evaluation-frequency 5m --window-size 15m \
  --condition "total ClientErrors > 50" \
  --action "$ACTION_GROUP"

# --- 5b. the Qdrant file share is thrashing again ---------------------
# qdrant-cc keeps its data directory on the `qdrant-storage` Azure File
# share (SMB, TransactionOptimized). Storage transactions are billed per
# operation, and Qdrant's index I/O can generate them in enormous
# volume when the optimizer is stuck.
#
# Measured daily Transactions on georagblobcc:
#     2026-08-17  10,785,865
#     2026-08-18   1,632,979
#     2026-08-19   1,243,868
#     2026-08-20   4,483,973
#     2026-08-21      45,357   <-- after the quota fix
#
# That last figure is the point. The share hit its old 10 GiB quota
# against ~7 GB used, the optimizer wedged ("Not enough space available
# for optimization"), and Qdrant retried in a loop. Raising the quota to
# 100 GiB on 2026-08-20 dropped transactions by a factor of ~240. So the
# transaction storm was a SYMPTOM of a fixed bug, not an inherent cost of
# running on SMB -- which is why this is an alert rather than a migration.
#
# Threshold at 2M/day: comfortably above the post-fix steady state and
# the ordinary 1.2-1.6M days, comfortably below a thrash.
run az monitor metrics alert create \
  -g "$RG" -n georagblobcc-transaction-storm \
  --scopes "/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.Storage/storageAccounts/georagblobcc" \
  --description "Storage transactions are far above the post-2026-08-20 baseline -- usually Qdrant's optimizer retrying against the qdrant-storage file share." \
  --severity 3 \
  --evaluation-frequency 1h --window-size 6h \
  --condition "total Transactions > 500000" \
  --action "$ACTION_GROUP"

# --- 5c. answer quality moved -----------------------------------------
# OBS-12. Refusal rate, hallucination-guard fire rate, zero-evidence rate
# and mean confidence are all recorded on silver.answer_runs and none of
# them was ever read. The `answer_quality_watch` Hatchet cron compares
# yesterday against the trailing week at 14:30 UTC and logs one line with
# the ANSWER_QUALITY_REGRESSION marker when something moved past its
# threshold.
#
# A log rule rather than a metric rule, because there is no metric: the
# production counters live on an unscraped Prometheus registry, and
# nothing ships them to Azure Monitor. The log line carries both windows'
# numbers, so this alert is actionable without opening a query.
#
# Sev 2, not 1. A refusal-rate spike is a quality problem, not an outage
# -- the system is up and answering, it is answering worse. Waking someone
# is the wrong response; seeing it the next morning is the right one.
#
# Deliberately NOT firing on `insufficient_sample`: on a quiet week the
# watch declines to compare, which is correct behaviour and not an alert.
run az monitor scheduled-query create   -g "$RG" -n answer-quality-regression   --scopes "$WS_ID"   --location "$LOCATION"   --description "Yesterday's refusal / guard-fire / zero-evidence / confidence signals moved past threshold against the trailing week. See ops/runbooks/refusal-rate-spike.md."   --severity 2   --evaluation-frequency 1h --window-size 1d   --condition "count 'regressions' > 0"   --condition-query regressions="ContainerAppConsoleLogs_CL
| where ContainerAppName_s == 'hatchet-worker-cc'
| where Log_s has 'ANSWER_QUALITY_REGRESSION'"   --action-groups "$ACTION_GROUP"

# --- 5d. cost burn past a workspace ceiling ---------------------------
# OBS-14. `cost_burn_watcher` detects a workspace spending past its cost
# ceiling, emits `cost.burn.alert` at severity "high", and at 2x the
# threshold suspends that workspace's LLM activity outright. Every one of
# those actions ended as a row in audit.audit_ledger plus an admin-surface
# broadcast -- which reaches nobody unless somebody is already logged into
# the admin UI looking at the alerts inbox.
#
# services/dispatchers/pagerduty.py looks like a second, real escalation
# path: a complete Events v2 client with dedup keys and a severity map. It
# has never had a caller, PAGERDUTY_INTEGRATION_KEY is empty, and there is
# no PagerDuty account. This rule is the escalation that exists.
#
# Sev 1, unlike 5c. A quality regression means the system is answering
# worse; this means money is leaving at a rate somebody set a ceiling to
# stop, and the next thing the watcher does is suspend the workspace.
#
# The log line carries the workspace id, both dollar figures and the
# window, so the alert is actionable without opening a query. It carries
# no workspace name and no query text.
run az monitor scheduled-query create \
  -g "$RG" -n cost-burn-threshold-exceeded \
  --scopes "$WS_ID" \
  --location "$LOCATION" \
  --description "A workspace spent past its cost ceiling. The watcher suspends LLM activity at 2x. See ops/runbooks/azure-oncall.md." \
  --severity 1 \
  --evaluation-frequency 15m --window-size 1h \
  --condition "count 'burns' > 0" \
  --condition-query burns="ContainerAppConsoleLogs_CL
| where ContainerAppName_s == 'hatchet-worker-cc'
| where Log_s has 'COST_BURN_THRESHOLD_EXCEEDED'" \
  --action-groups "$ACTION_GROUP"

# --- 5e. Qdrant lost points PG still counts as embedded -------------
# The embed sweep compares per-project counts: PG's embedded-passage count
# against Qdrant's exact count filtered by project_id. A gap over 2% means
# Qdrant dropped points that PG records as embedded -- qdrant-cc has no
# persistent volume, so a replica recreation mid-upsert does exactly this,
# and non-first batches upsert with wait=False and write embedding_id on
# acceptance rather than on flush.
#
# This is the failure mode with no other symptom. The collection is
# non-empty, so the all-empty self-heal does not fire; retrieval simply
# returns fewer hits and the answer is thinner, which reads as the corpus
# not covering the question. It was a log.warning until 2026-08-22.
#
# Sev 2. Nothing is down and no data is lost -- the source PDFs are intact
# and re-embedding recovers it -- but every answer drawn from that project
# is degraded until someone runs the reset script.
run az monitor scheduled-query create \
  -g "$RG" -n qdrant-partial-loss \
  --scopes "$WS_ID" \
  --location "$LOCATION" \
  --description "Qdrant is missing points that silver.document_passages records as embedded. Re-embed the named project with scripts/reset_embeddings_for_reencode.py." \
  --severity 2 \
  --evaluation-frequency 1h --window-size 6h \
  --condition "count 'gaps' > 0" \
  --condition-query gaps="ContainerAppConsoleLogs_CL
| where ContainerAppName_s == 'hatchet-worker-cc'
| where Log_s has 'QDRANT_PARTIAL_LOSS'" \
  --action-groups "$ACTION_GROUP"

# --- 6. Postgres logs (not an alert -- there is nothing to alert on) ---
# When georag-pg-cc-high-cpu fires at 03:00 there should be a slow query,
# a query store, autovacuum and connection-error evidence to look at, not
# just the CPU percentage that fired the alert.
#
# 2026-08-22: this step used to `create` a setting named pg-to-law, on the
# stated basis that `diagnostic-settings list` returned [] for this server.
# That is no longer true -- `georag-pg-audit` exists and already routes
# PostgreSQLLogs + PostgreSQLFlexSessions to the same workspace. Azure
# refuses a second setting sending the same category to the same sink
# ("Data sinks can't be reused"), so this create failed every time it ran.
#
# A resource gets one setting per sink, so the correct action is to widen
# the existing one, not add another. That is deliberately NOT automatic:
# enabling AllMetrics adds Log Analytics ingestion volume on a project
# that runs a cost-burn watcher, which is a spending decision.
existing_setting="$(
  az monitor diagnostic-settings list --resource "$PG_ID"     --query "[?workspaceId=='${WS_ID}'].name | [0]" -o tsv 2>/dev/null | strip_cr
)"

if [ -n "$existing_setting" ] && [ "$existing_setting" != "None" ]; then
  echo >&2
  echo "# NOTE: '${existing_setting}' already ships PG logs to this workspace," >&2
  echo "# so pg-to-law cannot be created alongside it. Currently enabled:" >&2
  az monitor diagnostic-settings show --name "$existing_setting" --resource "$PG_ID"     --query "{logs: logs[?enabled].category, metrics: metrics[?enabled].category}"     -o yaml >&2 2>/dev/null
  echo "# Widen that one instead of adding another. AllMetrics costs Log" >&2
  echo "# Analytics ingestion, so decide that before running the update." >&2
else
  run az monitor diagnostic-settings create     --name pg-to-law     --resource "$PG_ID"     --workspace "$WS_ID"     --logs '[{"category":"PostgreSQLLogs","enabled":true},{"category":"PostgreSQLFlexSessions","enabled":true},{"category":"PostgreSQLFlexQueryStoreRuntime","enabled":true}]'     --metrics '[{"category":"AllMetrics","enabled":true}]'
fi

# Pair rule 6 with a slow-query threshold, or PostgreSQLLogs will collect
# startup and checkpoint chatter and no queries:
#
#   az postgres flexible-server parameter set -g georag -s georag-pg-cc \
#     --name log_min_duration_statement --value 1000
#
# Left commented deliberately: it changes server behaviour rather than
# observability, and 1000ms is a guess until the query store has data.

if [ "$APPLY" -eq 0 ]; then
  echo "# dry run -- nothing was created. Re-run with --apply." >&2
fi
