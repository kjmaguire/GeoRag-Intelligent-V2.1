#!/usr/bin/env bash
# =============================================================================
# GeoRAG pgTAP — Golden MVT Snapshot Generator
# File: database/tests/pgtap/golden/generate.sh
# Module 8 Chunk 8.8 — Deliverable B
# =============================================================================
#
# Captures deterministic MVT bytes and etag_hash values from all 7 silver
# MVT functions using the GoldenFixture project at a fixed tile coordinate.
# Writes per-function .mvt binary files and a manifest.json for reference.
#
# The captured md5 hashes are baked into:
#   database/tests/pgtap/10_golden_mvt_snapshots.sql
#
# Usage:
#   1. Ensure the GeoRAG PostgreSQL container is running with migrations applied.
#   2. Run: bash database/tests/pgtap/golden/generate.sh
#   3. Review outputs in database/tests/pgtap/golden/
#   4. If hashes look correct, update 10_golden_mvt_snapshots.sql with any
#      changed hash values and commit both the manifest and the updated SQL.
#
# Regen required when:
#   - A silver MVT function body changes (logic or column list)
#   - The GoldenFixture seed data changes
#   - PostgreSQL or PostGIS major version upgrade changes MVT encoding
#
# DO NOT regen because of unrelated schema changes. Regen is a deliberate act
# that must be SME-reviewed before the new hashes are committed.
#
# Requirements:
#   - Docker Compose stack running with georag-postgresql healthy
#   - Seed fixture already loaded (run seed_golden_fixture.sql first)
#   - jq installed (for manifest.json formatting)
#
# Exit codes:
#   0 — all captures succeeded, manifest.json written
#   1 — one or more functions returned NULL mvt or unexpected error
#
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PGTAP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONTAINER="georag-postgresql"
PG_USER="georag"
PG_DB="georag"
PROJECT_ID="00000000-0000-0000-0000-deadbeefcafe"
TILE_Z=3
TILE_X=1
TILE_Y=2

# Colour codes (disabled when not a terminal)
if [ -t 1 ]; then
    GREEN="\033[0;32m"
    RED="\033[0;31m"
    YELLOW="\033[0;33m"
    RESET="\033[0m"
else
    GREEN="" RED="" YELLOW="" RESET=""
fi

# ── Verify container is running ───────────────────────────────────────────────
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER}$"; then
    echo -e "${RED}ERROR${RESET}: Container '${CONTAINER}' is not running." >&2
    echo "Start it with: docker compose up -d postgresql" >&2
    exit 1
fi

# ── Verify seed fixture is loaded ─────────────────────────────────────────────
COLLAR_COUNT=$(docker exec "${CONTAINER}" bash -c \
    "psql -t -A -U ${PG_USER} -d ${PG_DB} -c \
    \"SELECT COUNT(*) FROM silver.collars WHERE project_id = '${PROJECT_ID}';\"" 2>&1 | tr -d ' ')

if [[ "${COLLAR_COUNT}" -lt 1 ]]; then
    echo -e "${YELLOW}WARNING${RESET}: GoldenFixture project has no collars." >&2
    echo "Load the seed fixture first:" >&2
    echo "  bash database/tests/pgtap/run.sh --filter seed" >&2
    echo "  (or run seed_golden_fixture.sql directly in psql)" >&2
    exit 1
fi

echo "GoldenFixture seed verified: ${COLLAR_COUNT} collar(s) present."

# ── Capture function outputs ──────────────────────────────────────────────────
# Functions to capture: name → silver.function_name mapping
declare -A FUNCTIONS
FUNCTIONS=(
    ["collars"]="pg_collars_by_project"
    ["drill_traces"]="pg_drill_traces_by_project"
    ["seismic"]="pg_seismic_by_project"
    ["boundaries"]="pg_boundaries_by_project"
    ["formations"]="pg_formations_by_project"
    ["historic_workings"]="pg_historic_workings_by_project"
    ["geochem"]="pg_geochem_by_project"
)

# Ordered list for deterministic manifest output
FUNCTION_ORDER=(
    "collars"
    "drill_traces"
    "seismic"
    "boundaries"
    "formations"
    "historic_workings"
    "geochem"
)

FAILED=0
declare -A CAPTURED_MVT_MD5
declare -A CAPTURED_ETAG
declare -A CAPTURED_BYTES

for LAYER in "${FUNCTION_ORDER[@]}"; do
    FUNC_NAME="${FUNCTIONS[$LAYER]}"
    OUT_FILE="${SCRIPT_DIR}/${LAYER}.mvt"

    # Capture etag_hash and MVT md5 in a single query
    RESULT=$(docker exec "${CONTAINER}" bash -c \
        "psql -t -A -U ${PG_USER} -d ${PG_DB} -c \
        \"SELECT etag_hash || '|' || md5(mvt) || '|' || octet_length(mvt)::text \
          FROM silver.${FUNC_NAME}(${TILE_Z}, ${TILE_X}, ${TILE_Y}, \
          '{\\\"project_id\\\": \\\"${PROJECT_ID}\\\"}' ::json) \
          WHERE mvt IS NOT NULL;\"" 2>&1 | tr -d ' \r')

    if [[ -z "${RESULT}" ]]; then
        echo -e "${RED}FAIL${RESET} ${LAYER}: function returned NULL mvt — no data in tile or fixture missing"
        FAILED=1
        continue
    fi

    IFS='|' read -r ETAG MVT_MD5 MVT_BYTES <<< "${RESULT}"

    CAPTURED_ETAG["${LAYER}"]="${ETAG}"
    CAPTURED_MVT_MD5["${LAYER}"]="${MVT_MD5}"
    CAPTURED_BYTES["${LAYER}"]="${MVT_BYTES}"

    # Write MVT bytes to file using psql \copy
    docker exec "${CONTAINER}" bash -c \
        "psql -U ${PG_USER} -d ${PG_DB} -c \
        \"\\\\copy (SELECT mvt FROM silver.${FUNC_NAME}(${TILE_Z}, ${TILE_X}, ${TILE_Y}, \
          '{\\\"project_id\\\": \\\"${PROJECT_ID}\\\"}' ::json) WHERE mvt IS NOT NULL) \
          TO '/tmp/golden_${LAYER}.mvt' (FORMAT binary);\"" >/dev/null 2>&1 || true

    # Pull .mvt file from container (best-effort; not required for test assertions)
    docker cp "${CONTAINER}:/tmp/golden_${LAYER}.mvt" "${OUT_FILE}" >/dev/null 2>&1 && \
        echo -e "${GREEN}OK${RESET}  ${LAYER}: etag=${ETAG}  mvt_md5=${MVT_MD5}  bytes=${MVT_BYTES}" || \
        echo -e "${YELLOW}WARN${RESET} ${LAYER}: captured hash OK, .mvt binary copy failed (non-fatal)"

    echo -e "${GREEN}OK${RESET}  ${LAYER}: etag=${ETAG}  mvt_md5=${MVT_MD5}  bytes=${MVT_BYTES}"
done

if [[ ${FAILED} -eq 1 ]]; then
    echo -e "\n${RED}ERROR${RESET}: One or more functions failed. Manifest not written." >&2
    exit 1
fi

# ── Write manifest.json ───────────────────────────────────────────────────────
MANIFEST_FILE="${SCRIPT_DIR}/manifest.json"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

{
    echo "{"
    echo "  \"generated_at\": \"${TIMESTAMP}\","
    echo "  \"project_id\": \"${PROJECT_ID}\","
    echo "  \"tile\": {\"z\": ${TILE_Z}, \"x\": ${TILE_X}, \"y\": ${TILE_Y}},"
    echo "  \"tile_coverage\": \"lon -135 to -90, lat ~41 to ~67 (WGS84)\","
    echo "  \"note\": \"All etag_hash values are identical because they share md5(data_version|z|x|y|project_id). This is correct.\","
    echo "  \"layers\": {"
    FIRST=1
    for LAYER in "${FUNCTION_ORDER[@]}"; do
        [[ ${FIRST} -eq 0 ]] && echo ","
        FIRST=0
        echo -n "    \"${LAYER}\": {\"etag_hash\": \"${CAPTURED_ETAG[$LAYER]}\", \"mvt_md5\": \"${CAPTURED_MVT_MD5[$LAYER]}\", \"mvt_bytes\": ${CAPTURED_BYTES[$LAYER]}}"
    done
    echo ""
    echo "  }"
    echo "}"
} > "${MANIFEST_FILE}"

echo ""
echo "────────────────────────────────────────────────────────────────"
echo -e "${GREEN}DONE${RESET}: Manifest written to ${MANIFEST_FILE}"
echo ""
echo "Next steps:"
echo "  1. Review the manifest — confirm etag and mvt_md5 values look sane"
echo "  2. Have the SME verify at least one .mvt file in a tile viewer"
echo "  3. Commit: database/tests/pgtap/golden/manifest.json"
echo "  4. Update 10_golden_mvt_snapshots.sql if hashes changed"
echo ""
echo "Regen command:"
echo "  bash database/tests/pgtap/golden/generate.sh"
