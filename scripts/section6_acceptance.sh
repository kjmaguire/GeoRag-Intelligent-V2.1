#!/usr/bin/env bash
# =============================================================================
# scripts/section6_acceptance.sh
#
# Master-plan §6 (PublicGeo + density layer) — v1 acceptance harness.
# Mirrors scripts/phase_h4_acceptance.sh + scripts/section11_acceptance.sh.
#
# Pre-requisites:
#   - Docker compose stack up
#   - PG container reachable via docker exec
#   - Martin container reachable via docker exec
#
# Covers the §6-v1 surface:
#   §6.1   — public_geoscience.sources registry has rows
#   §6.4   — Public/Private Boundary Agent imports clean
#   §6.5   — silver.saved_map_views table present (Phase H4 dependency)
#   §6.6   — gold.h3_density_mineral table present with two indexes
#   §6.6   — Dagster asset module imports clean (no syntax errors)
#   §6.13  — Martin catalog includes density_choropleth_h3
#   §6.13  — Martin serves a non-empty MVT for a zoom-2 tile
#   §6.13  — Martin honors the ?commodity= URL filter
#   h3     — h3 + h3_postgis extensions enabled
#
# Out of scope (deferred to §6-v2):
#   - Frontend layer-pack composition (6.7)
#   - MapView density layer toggle (6.8 / mvtLayers.ts edit)
#   - Feature Inspector / AOI / Evidence Map Mode (6.9-6.12)
#
# Exit 0 = §6-v1 surface green. 1 = at least one check failed.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=0
FAILED=()

PG_CONTAINER="${PG_CONTAINER:-georag-postgresql}"
MARTIN_CONTAINER="${MARTIN_CONTAINER:-georag-martin}"
FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"
PG_USER="${PG_USER:-georag}"
PG_DB="${PG_DB:-georag}"

psql_q() {
    docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
}

martin_q() {
    docker exec "$MARTIN_CONTAINER" sh -c "wget -qO- 'http://localhost:3000$1' 2>/dev/null"
}

martin_status() {
    # -a flag forces grep to treat binary input as text — MVT tile bodies
    # contain null bytes that otherwise cause grep to silently skip the
    # match (returns empty + reports "binary file matches" to stderr).
    docker exec "$MARTIN_CONTAINER" sh -c "wget -qSO- 'http://localhost:3000$1' 2>&1 | grep -aoE 'HTTP/[0-9.]+ [0-9]+' | head -1 | awk '{print \$2}'"
}

check() {
    local label="$1"
    local cond="$2"
    TOTAL=$((TOTAL + 1))
    if [ "$cond" = "true" ] || [ "$cond" = "1" ]; then
        echo "  [PASS] $label"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $label"
        FAILED+=("$label")
    fi
}

echo
echo "=============================================================="
echo "  Master-plan §6 acceptance harness — v1 surface"
echo "  Targets: pg=$PG_CONTAINER  martin=$MARTIN_CONTAINER"
echo "=============================================================="
echo

# ----------------------------------------------------------------------------
# 1. §6.1 — public_geoscience.sources registry
# ----------------------------------------------------------------------------
echo "-- §6.1 public_geoscience registry --"
SRC_COUNT=$(psql_q "SELECT count(*) FROM public_geoscience.sources;" | head -1 | tr -d ' ')
TOTAL=$((TOTAL + 1))
if [ -n "$SRC_COUNT" ] && [ "$SRC_COUNT" -ge 5 ]; then
    echo "  [PASS] public_geoscience.sources has $SRC_COUNT rows (≥5 expected)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] public_geoscience.sources has $SRC_COUNT rows (need ≥5; rerun ingest)"
    FAILED+=("sources registry empty")
fi

# ----------------------------------------------------------------------------
# 2. §6.4 — Public/Private Boundary Agent imports clean
# ----------------------------------------------------------------------------
echo
echo "-- §6.4 Public/Private Boundary Agent --"
TOTAL=$((TOTAL + 1))
if docker exec "$FASTAPI_CONTAINER" python -c "from app.agents.phase6.public_private_boundary import public_private_boundary; assert callable(public_private_boundary)" 2>/dev/null; then
    echo "  [PASS] public_private_boundary callable + importable"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] public_private_boundary import broken"
    FAILED+=("boundary agent import")
fi

# ----------------------------------------------------------------------------
# 3. §6.5 — saved_map_views table (Phase H4 dependency)
# ----------------------------------------------------------------------------
echo
echo "-- §6.5 saved_map_views --"
TBL_SMV=$(psql_q "SELECT 1 FROM information_schema.tables WHERE table_schema='silver' AND table_name='saved_map_views';" | head -1 | tr -d ' ')
check "silver.saved_map_views present" "$TBL_SMV"

# ----------------------------------------------------------------------------
# 4. §6.6 — gold.h3_density_mineral table + indexes
# ----------------------------------------------------------------------------
echo
echo "-- §6.6 h3 density table + indexes --"
TBL_H3=$(psql_q "SELECT 1 FROM information_schema.tables WHERE table_schema='gold' AND table_name='h3_density_mineral';" | head -1 | tr -d ' ')
check "gold.h3_density_mineral present" "$TBL_H3"

IDX_H3=$(psql_q "SELECT count(*) FROM pg_indexes WHERE schemaname='gold' AND indexname IN ('idx_h3_density_resolution_commodity','idx_h3_density_h3');" | head -1 | tr -d ' ')
TOTAL=$((TOTAL + 1))
if [ "$IDX_H3" = "2" ]; then
    echo "  [PASS] gold.h3_density_mineral indexes (2/2)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] gold.h3_density_mineral indexes — found $IDX_H3/2"
    FAILED+=("h3 density indexes incomplete")
fi

# Dagster asset module imports
TOTAL=$((TOTAL + 1))
if docker exec "$FASTAPI_CONTAINER" python -c "import ast; ast.parse(open('/dev/null').read() if False else open('/var/dagster_assets/gold_h3_density.py', 'r').read() if False else 'pass')" 2>/dev/null; then
    : # placeholder skip — fastapi doesn't have the dagster path
fi
# Better: just AST-parse from the host
if python -c "import ast; ast.parse(open('src/dagster/georag_dagster/assets/gold_h3_density.py').read())" 2>/dev/null; then
    echo "  [PASS] gold_h3_density.py AST parses clean"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] gold_h3_density.py AST parse failed"
    FAILED+=("gold_h3_density.py syntax")
fi

# ----------------------------------------------------------------------------
# 5. §6.13 — Martin density choropleth function
# ----------------------------------------------------------------------------
echo
echo "-- §6.13 Martin density choropleth --"
CATALOG=$(martin_q "/catalog")
TOTAL=$((TOTAL + 1))
if echo "$CATALOG" | grep -q '"density_choropleth_h3"'; then
    echo "  [PASS] Martin catalog includes density_choropleth_h3"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Martin catalog missing density_choropleth_h3"
    FAILED+=("martin catalog missing density")
fi

STATUS_Z2=$(martin_status "/density_choropleth_h3/2/0/1")
TOTAL=$((TOTAL + 1))
if [ "$STATUS_Z2" = "200" ]; then
    echo "  [PASS] Martin returns 200 for z=2 tile"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Martin returns $STATUS_Z2 for z=2 tile"
    FAILED+=("martin z=2 status")
fi

# Non-empty tile
TILE_BYTES=$(docker exec "$MARTIN_CONTAINER" sh -c "wget -qO- 'http://localhost:3000/density_choropleth_h3/2/0/1' 2>/dev/null | wc -c" 2>/dev/null | tr -d ' ')
TOTAL=$((TOTAL + 1))
if [ -n "$TILE_BYTES" ] && [ "$TILE_BYTES" -gt 0 ]; then
    echo "  [PASS] Martin z=2 tile has $TILE_BYTES bytes"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Martin z=2 tile is empty"
    FAILED+=("martin z=2 empty tile")
fi

# Commodity filter changes byte count (must produce a smaller subset)
FILT_BYTES=$(docker exec "$MARTIN_CONTAINER" sh -c "wget -qO- 'http://localhost:3000/density_choropleth_h3/2/0/1?commodity=au' 2>/dev/null | wc -c" 2>/dev/null | tr -d ' ')
TOTAL=$((TOTAL + 1))
if [ -n "$FILT_BYTES" ] && [ -n "$TILE_BYTES" ] && [ "$FILT_BYTES" -lt "$TILE_BYTES" ]; then
    echo "  [PASS] ?commodity=au filter narrows bytes ($FILT_BYTES < $TILE_BYTES)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] ?commodity=au filter did NOT narrow (got $FILT_BYTES vs $TILE_BYTES)"
    FAILED+=("commodity filter ineffective")
fi

# ----------------------------------------------------------------------------
# 6. h3 extensions
# ----------------------------------------------------------------------------
echo
echo "-- h3 extensions --"
H3_COUNT=$(psql_q "SELECT count(*) FROM pg_extension WHERE extname IN ('h3','h3_postgis');" | head -1 | tr -d ' ')
TOTAL=$((TOTAL + 1))
if [ "$H3_COUNT" = "2" ]; then
    echo "  [PASS] h3 + h3_postgis extensions both enabled"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] h3 extensions count = $H3_COUNT (expected 2)"
    FAILED+=("h3 extensions missing")
fi

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
echo
echo "=============================================================="
echo "  §6 v1 acceptance: $PASS / $TOTAL checks passed"
if [ ${#FAILED[@]} -ne 0 ]; then
    echo "  Failures:"
    for f in "${FAILED[@]}"; do
        echo "    - $f"
    done
    echo "=============================================================="
    exit 1
fi
echo "  §6-v1 surface green. Frontend layer-pack work deferred to v2."
echo "=============================================================="
exit 0
