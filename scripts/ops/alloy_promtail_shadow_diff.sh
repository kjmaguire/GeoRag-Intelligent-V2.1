#!/usr/bin/env bash
# =============================================================================
# alloy_promtail_shadow_diff.sh — Promtail → Grafana Alloy cutover decision tool
# =============================================================================
# Part of the Promtail -> Alloy migration (ops/runbooks/promtail-to-alloy-
# migration.md). During the shadow window, both Promtail and Alloy ship the
# same Docker + authz_audit logs to Loki — Promtail to the default/`fake`
# tenant, Alloy to the `alloy-shadow` tenant (set via the tenant_id line in
# docker/alloy/config.alloy).
#
# This script queries Loki over a window and compares the two tenants on the
# three things that decide whether Alloy is a faithful drop-in:
#   1. Total log lines ingested per stream  (must match within TOLERANCE_PCT)
#   2. Distinct label sets / cardinality     (must match exactly)
#   3. Presence of the load-bearing labels   (authz_audit: event/reason/
#      target_workspace_id; docker: container/service/traceparent)
#
# Exit 0 + "CUTOVER: GREEN" when Alloy matches Promtail within tolerance on all
# three. Non-zero + "CUTOVER: REVIEW" otherwise, with the diffs printed.
#
# Usage:
#   scripts/ops/alloy_promtail_shadow_diff.sh [WINDOW] [LOKI_URL]
#     WINDOW    Loki range to compare (default: 1h). e.g. 6h, 24h.
#     LOKI_URL  default: http://localhost:3100
#
# Env:
#   PROMTAIL_TENANT   default: fake     (Loki's default no-auth org id)
#   ALLOY_TENANT      default: alloy-shadow
#   TOLERANCE_PCT     default: 1        (allowed line-count delta %)
#
# Requires: curl, jq.
# =============================================================================
set -euo pipefail

WINDOW="${1:-1h}"
LOKI_URL="${2:-http://localhost:3100}"
PROMTAIL_TENANT="${PROMTAIL_TENANT:-fake}"
ALLOY_TENANT="${ALLOY_TENANT:-alloy-shadow}"
TOLERANCE_PCT="${TOLERANCE_PCT:-1}"

command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not installed"; exit 2; }

# Loki instant query for a metric over the window, scoped to one tenant.
# Returns a single scalar (sum). Uses the X-Scope-OrgID header for tenant.
loki_count() {
  local tenant="$1" selector="$2"
  curl -sf -G "${LOKI_URL}/loki/api/v1/query" \
    -H "X-Scope-OrgID: ${tenant}" \
    --data-urlencode "query=sum(count_over_time(${selector}[${WINDOW}]))" \
    | jq -r '.data.result[0].value[1] // "0"'
}

# Distinct label-value pairs seen on a label key for a tenant over the window.
loki_label_values() {
  local tenant="$1" label="$2"
  curl -sf -G "${LOKI_URL}/loki/api/v1/label/${label}/values" \
    -H "X-Scope-OrgID: ${tenant}" \
    --data-urlencode "start=$(date -u -d "-${WINDOW}" +%s)000000000" 2>/dev/null \
    | jq -r '.data // [] | sort | join(",")'
}

echo "=== Promtail -> Alloy shadow diff (window=${WINDOW}, loki=${LOKI_URL}) ==="
echo "    promtail tenant=${PROMTAIL_TENANT}  alloy tenant=${ALLOY_TENANT}  tolerance=${TOLERANCE_PCT}%"
echo

RESULT="GREEN"

# ---- 1. Line counts per logical stream -------------------------------------
for sel in '{job="docker"}' '{job="authz_audit"}' '{service="fastapi"}' '{service="laravel"}'; do
  p=$(loki_count "${PROMTAIL_TENANT}" "${sel}")
  a=$(loki_count "${ALLOY_TENANT}" "${sel}")
  # delta % (guard divide-by-zero)
  if [ "${p%.*}" -eq 0 ] 2>/dev/null; then
    [ "${a%.*}" -eq 0 ] 2>/dev/null && d=0 || d=100
  else
    d=$(awk -v p="$p" -v a="$a" 'BEGIN{printf "%.1f", (a-p)/p*100}')
  fi
  ad=$(awk -v d="$d" 'BEGIN{print (d<0?-d:d)}')
  ok=$(awk -v ad="$ad" -v t="$TOLERANCE_PCT" 'BEGIN{print (ad<=t)?"ok":"DIFF"}')
  [ "$ok" = "DIFF" ] && RESULT="REVIEW"
  printf "  lines %-26s promtail=%-10s alloy=%-10s delta=%6s%%  %s\n" "$sel" "$p" "$a" "$d" "$ok"
done
echo

# ---- 2 + 3. Label-set parity on the load-bearing labels --------------------
for lbl in container service event reason target_workspace_id traceparent level; do
  pv=$(loki_label_values "${PROMTAIL_TENANT}" "${lbl}")
  av=$(loki_label_values "${ALLOY_TENANT}" "${lbl}")
  if [ "$pv" = "$av" ]; then
    printf "  label %-22s MATCH (%s values)\n" "$lbl" "$(echo "$pv" | awk -F, 'NF{print NF}END{if(NR==0)print 0}')"
  else
    RESULT="REVIEW"
    printf "  label %-22s MISMATCH\n      promtail: %s\n      alloy:    %s\n" "$lbl" "${pv:-<none>}" "${av:-<none>}"
  fi
done

echo
if [ "$RESULT" = "GREEN" ]; then
  echo "CUTOVER: GREEN — Alloy matches Promtail within ${TOLERANCE_PCT}% on line counts"
  echo "  and exactly on label parity. Safe to promote per the runbook:"
  echo "    1. delete the tenant_id line in docker/alloy/config.alloy"
  echo "    2. stop the promtail service (comment it out in docker-compose.yml)"
  echo "    3. keep promtail commented for one week as rollback, then remove."
  exit 0
else
  echo "CUTOVER: REVIEW — diffs above. Do NOT promote until resolved."
  echo "  Common causes: relabel-rule defaults (empty-label handling), a"
  echo "  pipeline stage that drops/keeps a label differently, or Alloy still"
  echo "  warming up its file positions (let it run longer)."
  exit 1
fi
