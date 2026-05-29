#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_step1_verify.sh
#
# Phase 0 Step 1 done-definition (per georag-phase0-implementation-kickoff.md).
# Run from inside WSL after the canonical docker-compose stack is up.
# Exits 0 only if all 7 checks pass.
#
# Notes on spec deviations vs kickoff doc (locked 2026-05-09):
#   - public_geo namespace matches the spec exactly (rename from
#     public_geoscience executed 2026-05-17 via ALTER SCHEMA — see
#     database/migrations/2026_05_17_120100_rename_public_geoscience_to_public_geo.php).
#   - SeaweedFS three tiers are logical buckets (tier-hot/warm/cold), not
#     physical disk separation — physical sep is Phase 11 hardening.
#   - vLLM metric prefix is `vllm:` (Prometheus namespace), not `vllm_` as the
#     kickoff doc grep pattern expects.
#   - Hatchet readiness endpoint is `/api/ready`, not `/v1/healthz`.
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

REDIS_PASSWORD=$(grep -E '^REDIS_PASSWORD=' ${HERE}/../.env | cut -d= -f2-)

PASS=0
TOTAL=7

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cat <<'BANNER'

============================================================
PHASE 0 STEP 1 — DONE-DEFINITION VERIFICATION
============================================================
BANNER

# 1) PostgreSQL 18+
v=$($PG_PSQL_BIN -tAc \
    "SELECT regexp_replace(version(), '.*PostgreSQL ([0-9]+).*', E'\\\\1')" 2>/dev/null)
v="${v// /}"
if [ "$v" -ge 18 ] 2>/dev/null; then check "PostgreSQL 18+ (got $v)" ok; else check "PostgreSQL 18+" fail "got [$v]"; fi

# 2) 10 required extensions installed
n=$($PG_PSQL_BIN -tAc \
    "SELECT count(*) FROM pg_extension WHERE extname IN ('postgis','pg_trgm','pg_stat_statements','auto_explain','h3','hypopg','pg_stat_kcache','pg_partman','pg_repack','pg_ivm');")
n="${n// /}"
if [ "$n" = "10" ]; then check "10/10 PG extensions installed" ok; else check "PG extensions" fail "got $n / 10"; fi

# 3) 8 schema namespaces present
n=$($PG_PSQL_BIN -tAc \
    "SELECT count(*) FROM pg_namespace WHERE nspname IN ('audit','usage','silver','gold','public_geo','outbox','workflow','workspace');")
n="${n// /}"
if [ "$n" = "8" ]; then check "8/8 schema namespaces present" ok; else check "schema namespaces" fail "got $n / 8"; fi

# 4) SeaweedFS three named tier buckets
t=$(docker exec georag-minio sh -c 'echo s3.bucket.list | weed shell -master=localhost:9333' 2>/dev/null | grep -cE 'tier-(hot|warm|cold)')
if [ "$t" = "3" ]; then check "SeaweedFS tier-hot/warm/cold buckets exist" ok; else check "SeaweedFS tier buckets" fail "got $t / 3"; fi

# 5) Redis reachable
r=$(docker exec georag-redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning PING 2>&1)
if [ "$r" = "PONG" ]; then check "Redis PING → PONG" ok; else check "Redis PING" fail "$r"; fi

# 6) Hatchet engine reachable
h=$(curl -s -o /dev/null -w '%{http_code}' ${HATCHET_URL}/api/ready)
if [ "$h" = "200" ]; then check "Hatchet engine /api/ready → 200" ok; else check "Hatchet /api/ready" fail "http $h"; fi

# 7) vLLM Prometheus metrics scrapeable (vllm:* lines present)
m=$(curl -s http://localhost:8001/metrics 2>/dev/null | grep -cE '^vllm:')
if [ "$m" -gt 0 ]; then check "vLLM Prometheus scrape (vllm:* lines = $m)" ok; else check "vLLM metrics" fail "got 0 vllm:* lines"; fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

if [ "$PASS" = "$TOTAL" ]; then
    exit 0
else
    exit 1
fi
