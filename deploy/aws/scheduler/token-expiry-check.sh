#!/usr/bin/env bash
# Daily check of how long HATCHET_CLIENT_TOKEN has left.
#
# Runs as a one-off ECS task launched by the `georag-token-check` EventBridge
# schedule (deploy/aws/terraform/scheduler.tf), with the token injected from
# georag/app by the execution role, the same way every application task gets
# it. The script makes NO AWS calls and needs no task role.
#
# WHY (audit AWS-12, 2026-09-29). The engine mints the token with a 90-day
# lifetime (`exp - iat` = 7776000 s, deploy/aws/README.md Step 3), and nothing
# renewed it or alarmed on it. When it lapses, every worker and client fails
# auth at the same moment while the engine looks healthy: all 50 workflows
# and every cron stop, one quiet morning about three months after go-live.
#
# WHAT IT SAYS. Every run logs one line with the days left. Inside
# TOKEN_WARN_DAYS (default 21), or when the expiry cannot be read at all, it
# logs a HATCHET_TOKEN_EXPIRING line instead. alerts.tf's
# `hatchet-token-expiring` alarm emails on that marker, so the reminder
# repeats every day until deploy/aws/rotation/rotate-hatchet-token.sh has
# run. A check that cannot read the expiry says so through the SAME marker:
# a silent failure here would be exactly the gap it exists to close.
#
# THE TOKEN IS NEVER PRINTED — not the token, not its payload. Only the
# number of days and the expiry date. Progress goes to stderr, for the reason
# shutdown-sweep.sh's header gives.
#
# Test hooks: TOKEN_CHECK_NOW overrides the clock (epoch seconds).
set -uo pipefail

WARN_DAYS="${TOKEN_WARN_DAYS:-21}"
NOW="${TOKEN_CHECK_NOW:-$(date +%s)}"

log()   { printf '%s\n' "$*" >&2; }
alert() { log "HATCHET_TOKEN_EXPIRING $*"; }

token="${HATCHET_CLIENT_TOKEN:-}"
unset HATCHET_CLIENT_TOKEN
if [ -z "$token" ]; then
  alert "cannot check: HATCHET_CLIENT_TOKEN is not in this task's environment"
  exit 1
fi

# header.payload.signature — only the payload is read.
payload=""
IFS=. read -r _ payload _ <<<"$token"
unset token
if [ -z "$payload" ]; then
  alert "cannot check: the token is not a JWT (no payload segment)"
  exit 1
fi

# base64url -> base64, re-padded.
b64=$(printf '%s' "$payload" | tr '_-' '/+')
case $(( ${#b64} % 4 )) in
  2) b64="${b64}==" ;;
  3) b64="${b64}=" ;;
esac
claims=$(printf '%s' "$b64" | base64 -d 2>/dev/null) || claims=""
unset payload b64

# bash's own regex, so the check needs nothing beyond coreutils' base64.
if [[ "$claims" =~ \"exp\"[[:space:]]*:[[:space:]]*([0-9]+) ]]; then
  exp="${BASH_REMATCH[1]}"
else
  alert "cannot check: no exp claim could be read from the token (placeholder token, or base64 missing from the image)"
  exit 1
fi
unset claims

days=$(( (exp - NOW) / 86400 ))
when=$(date -u -d "@${exp}" +%Y-%m-%d 2>/dev/null || printf 'epoch %s' "$exp")

if [ "$exp" -le "$NOW" ]; then
  alert "EXPIRED on ${when} — every Hatchet worker and client is failing auth. Rotate now: deploy/aws/rotation/rotate-hatchet-token.sh"
  exit 1
fi
if [ "$days" -lt "$WARN_DAYS" ]; then
  alert "${days} day(s) left, expires ${when}. Rotate before then: deploy/aws/rotation/rotate-hatchet-token.sh (ops/runbooks/secret-rotation.md §9)"
  exit 0
fi

log "hatchet client token: ${days} day(s) left, expires ${when}"
exit 0
