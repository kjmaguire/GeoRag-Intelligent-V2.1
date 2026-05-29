#!/usr/bin/env bash
# =============================================================================
# GeoRAG pgTAP runner — Module 8 Chunk 8.8
# =============================================================================
#
# Executes pgTAP assertion files against the georag database inside the
# running georag-postgresql container.
#
# Requirements:
#   - Docker Compose stack running with georag-postgresql healthy
#   - pgTAP 1.3.3 installed in the georag database
#     (pg_prove is NOT required — this runner parses psql output directly)
#
# Usage:
#   ./database/tests/pgtap/run.sh               # run all *.sql files
#   ./database/tests/pgtap/run.sh --filter 08   # run only files matching "08"
#
# Exit codes:
#   0 — all assertions passed
#   1 — one or more assertions failed or a SQL error occurred
#
# Output format:
#   <file>: N of M ok
#   SUMMARY: X of Y ok
#   (non-zero exit if any failures)
#
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
CONTAINER="georag-postgresql"
PG_USER="georag"
PG_DB="georag"

# Colour codes (disabled when not a terminal)
if [ -t 1 ]; then
    GREEN="\033[0;32m"
    RED="\033[0;31m"
    YELLOW="\033[0;33m"
    RESET="\033[0m"
else
    GREEN="" RED="" YELLOW="" RESET=""
fi

# ── Argument parsing ──────────────────────────────────────────────────────────
FILTER=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --filter)
            FILTER="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--filter <pattern>]" >&2
            exit 1
            ;;
    esac
done

# ── Verify container is running ───────────────────────────────────────────────
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER}$"; then
    echo -e "${RED}ERROR${RESET}: Container '${CONTAINER}' is not running." >&2
    echo "Start it with: docker compose up -d postgresql" >&2
    exit 1
fi

# ── Collect test files ────────────────────────────────────────────────────────
# Only pick up numbered pgTAP files (NN_*.sql). Plain SQL files like
# seed_golden_fixture.sql are excluded — they have no pgTAP plan() call.
mapfile -t SQL_FILES < <(find "${SCRIPT_DIR}" -maxdepth 1 -name "[0-9][0-9]_*.sql" | sort)
if [[ -n "${FILTER}" ]]; then
    mapfile -t SQL_FILES < <(printf '%s\n' "${SQL_FILES[@]}" | grep "${FILTER}")
fi

if [[ ${#SQL_FILES[@]} -eq 0 ]]; then
    echo -e "${YELLOW}WARNING${RESET}: No SQL files found matching filter '${FILTER}'" >&2
    exit 0
fi

# ── Run each file ─────────────────────────────────────────────────────────────
TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_FILES=0
FAILED_FILES=()

for SQL_FILE in "${SQL_FILES[@]}"; do
    BASENAME="$(basename "${SQL_FILE}")"
    CONTAINER_PATH="/tmp/pgtap_run_${BASENAME}"

    # Copy file into container
    docker cp "${SQL_FILE}" "${CONTAINER}:${CONTAINER_PATH}" >/dev/null

    # Run via psql in tuples-only mode (-t) for clean TAP output
    RAW_OUTPUT=$(docker exec "${CONTAINER}" bash -c \
        "psql -t -U ${PG_USER} -d ${PG_DB} -f ${CONTAINER_PATH} 2>&1")

    # Remove temp file from container
    docker exec "${CONTAINER}" rm -f "${CONTAINER_PATH}" 2>/dev/null || true

    # Parse plan line for total expected assertions
    PLAN_TOTAL=$(echo "${RAW_OUTPUT}" | grep -m1 '^ 1\.\.' | sed 's/^ 1\.\.//' | tr -d ' ' || echo "?")

    # Count ok / not ok lines (visible SELECT output; DO block assertions are
    # registered internally by pgTAP but not printed as rows in -t mode).
    # Use plan total as authoritative pass count when finish() shows no failures.
    PASS_COUNT=$(echo "${RAW_OUTPUT}" | grep -c '^ ok [0-9]' || true)
    FAIL_COUNT=$(echo "${RAW_OUTPUT}" | grep -c '^ not ok [0-9]' || true)

    # Check for SQL errors (transaction aborts)
    SQL_ERROR=$(echo "${RAW_OUTPUT}" | grep -c 'ERROR:' || true)

    # Check finish() output
    FINISH_FAILED=$(echo "${RAW_OUTPUT}" | grep 'Looks like you failed' || true)
    FINISH_PLANNED=$(echo "${RAW_OUTPUT}" | grep 'planned .* but ran' || true)

    FILE_STATUS="ok"
    if [[ ${FAIL_COUNT} -gt 0 ]] || [[ ${SQL_ERROR} -gt 0 ]] || \
       [[ -n "${FINISH_FAILED}" ]] || [[ -n "${FINISH_PLANNED}" ]]; then
        FILE_STATUS="fail"
    fi

    TOTAL_PASS=$((TOTAL_PASS + PASS_COUNT))
    TOTAL_FAIL=$((TOTAL_FAIL + FAIL_COUNT))
    TOTAL_FILES=$((TOTAL_FILES + 1))

    if [[ "${FILE_STATUS}" == "ok" ]]; then
        # When all pass, report PLAN_TOTAL (authoritative) rather than visible ok count
        # (DO block assertions register internally but don't print in -t mode)
        echo -e "${GREEN}PASS${RESET} ${BASENAME}: ${PLAN_TOTAL} of ${PLAN_TOTAL} ok"
        TOTAL_PASS=$((TOTAL_PASS + PLAN_TOTAL - PASS_COUNT))  # credit the DO-block assertions
    else
        echo -e "${RED}FAIL${RESET} ${BASENAME}: ${PASS_COUNT} of ${PLAN_TOTAL} ok, ${FAIL_COUNT} not ok"
        FAILED_FILES+=("${BASENAME}")

        # Print failing test lines and any SQL errors
        echo "${RAW_OUTPUT}" | grep -E '^ not ok|ERROR:|# Failed|Looks like' | \
            sed 's/^/  /' || true
    fi
done

# ── Summary ───────────────────────────────────────────────────────────────────
TOTAL=$((TOTAL_PASS + TOTAL_FAIL))
echo ""
echo "────────────────────────────────────────"
if [[ ${#FAILED_FILES[@]} -eq 0 ]]; then
    echo -e "${GREEN}SUMMARY${RESET}: ${TOTAL_PASS} of ${TOTAL} ok — all ${TOTAL_FILES} file(s) passed"
    exit 0
else
    echo -e "${RED}SUMMARY${RESET}: ${TOTAL_PASS} of ${TOTAL} ok — ${#FAILED_FILES[@]} file(s) FAILED:"
    for F in "${FAILED_FILES[@]}"; do
        echo "  - ${F}"
    done
    exit 1
fi
