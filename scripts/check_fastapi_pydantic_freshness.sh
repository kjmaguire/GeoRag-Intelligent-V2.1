#!/usr/bin/env bash
# =============================================================================
# scripts/check_fastapi_pydantic_freshness.sh
#
# Phase 4 Step 3 — catch the "stale fastapi Pydantic input model" footgun
# (R-P3-3 from the Phase 3 handoff).
#
# Why this exists: Pydantic model classes get cached at module-import
# time inside the fastapi process. When a Hatchet workflow file gains
# a new field (e.g. `signature` in external_notification.py at Phase 3
# Step 5), the on-disk model HAS the field but the running fastapi
# process still serializes / validates against the OLD model — silently
# dropping the new field on `model_validate()`. Reproduces only after
# code mutation + before `docker compose restart fastapi`.
#
# The script compares mtimes of every Python file the fastapi process
# imports for workflow IO against the fastapi container's start time.
# Any file newer than the container start → fastapi needs a restart.
#
# Exit code 0 = fresh, 1 = stale, 2 = couldn't inspect container.
#
# Usage:
#   scripts/check_fastapi_pydantic_freshness.sh           # one-shot check
#   scripts/check_fastapi_pydantic_freshness.sh --quiet   # exit-code only
#
# Wire into `composer test` (or equivalent) so a stale fastapi surfaces
# before any smoke does.
# =============================================================================

set -uo pipefail

CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"
REPO_ROOT="${REPO_ROOT:-/home/georag/projects/georag}"

QUIET=0
if [ "${1:-}" = "--quiet" ]; then
    QUIET=1
fi

log() {
    [ "$QUIET" = "1" ] || echo "$@"
}

WATCH_PATHS=(
    "$REPO_ROOT/src/fastapi/app/hatchet_workflows"
    "$REPO_ROOT/src/fastapi/app/services"
    "$REPO_ROOT/src/fastapi/app/routers"
)

started_at=$(docker inspect --format='{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null)
if [ -z "$started_at" ]; then
    log "ERROR: container $CONTAINER not running (or not inspectable)"
    exit 2
fi

started_epoch=$(date -d "$started_at" +%s 2>/dev/null)
if [ -z "$started_epoch" ]; then
    log "ERROR: could not parse container start time '$started_at'"
    exit 2
fi

started_iso=$(date -d "@$started_epoch" -Iseconds)
log "fastapi container started at: $started_iso"
log

stale_count=0
declare -a stale_files=()

for p in "${WATCH_PATHS[@]}"; do
    if [ ! -d "$p" ]; then
        continue
    fi
    while IFS= read -r -d '' f; do
        # Skip __pycache__ etc.
        case "$f" in *__pycache__*) continue ;; esac
        mtime=$(stat -c '%Y' "$f" 2>/dev/null) || continue
        if [ "$mtime" -gt "$started_epoch" ]; then
            stale_files+=("$f|$mtime")
            stale_count=$((stale_count + 1))
        fi
    done < <(find "$p" -name '*.py' -type f -print0 2>/dev/null)
done

if [ "$stale_count" -gt 0 ]; then
    log "STALE — $stale_count file(s) newer than fastapi container start:"
    for entry in "${stale_files[@]}"; do
        file="${entry%%|*}"
        mtime="${entry##*|}"
        mtime_iso=$(date -d "@$mtime" -Iseconds 2>/dev/null)
        log "  $file"
        log "    mtime  = $mtime_iso"
    done
    log
    log "Fix: docker compose restart fastapi"
    log "     (or: docker compose -f $REPO_ROOT/docker-compose.yml restart fastapi)"
    exit 1
fi

log "OK — no fastapi-imported source is newer than the running container."
exit 0
