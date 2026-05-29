#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_step1_verify.sh
#
# Phase 3 Step 1 done-definition — `kestra` role + logical database.
#
#   1. kestra role exists, NOSUPERUSER + NOBYPASSRLS + LOGIN
#   2. kestra database exists, owned by kestra
#   3. kestra role can authenticate + connect to its DB
#   4. kestra role CANNOT read georag schemas (cross-DB blast radius)
#   5. No table grants leaked into georag's app schemas
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5

CONTAINER="${POSTGRES_CONTAINER:-georag-postgresql}"
PG_USER="${POSTGRES_USER:-georag}"
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KESTRA_PASSWORD=$(awk -F= '/^KESTRA_PG_PASSWORD=/ { print $2 }' "$ENVFILE" 2>/dev/null | head -1)

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

q() {
    docker exec "$CONTAINER" psql -U "$PG_USER" -d georag -tAc "$1" 2>/dev/null
}

cat <<'BANNER'

============================================================
PHASE 3 STEP 1 — kestra ROLE + DB VERIFICATION
============================================================
BANNER

# 1) Role posture
posture=$(q "
    SELECT (CASE WHEN rolsuper      THEN 'super'   ELSE 'nosuper'   END) || '/' ||
           (CASE WHEN rolbypassrls  THEN 'bypass'  ELSE 'nobypass'  END) || '/' ||
           (CASE WHEN rolcanlogin   THEN 'login'   ELSE 'nologin'   END)
    FROM pg_roles WHERE rolname = 'kestra';")
case "$posture" in
    nosuper/nobypass/login) check "kestra role: NOSUPERUSER + NOBYPASSRLS + LOGIN" ok ;;
    "")                     check "kestra role exists"           fail "role missing" ;;
    *)                      check "kestra role posture"          fail "got '$posture'" ;;
esac

# 2) DB ownership
db_owner=$(q "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='kestra';")
[ "$db_owner" = "kestra" ] \
    && check "kestra database exists, owned by kestra" ok \
    || check "kestra DB ownership" fail "got '$db_owner'"

# 3) Authenticate + connect
if [ -z "$KESTRA_PASSWORD" ]; then
    check "kestra can connect" fail "no KESTRA_PG_PASSWORD in .env"
else
    auth_ok=$(docker exec -e PGPASSWORD="$KESTRA_PASSWORD" "$CONTAINER" \
        psql -U kestra -d kestra -tAc 'SELECT 1' 2>&1)
    [ "$auth_ok" = "1" ] \
        && check "kestra can connect to kestra DB" ok \
        || check "kestra login" fail "$(echo "$auth_ok" | head -1)"
fi

# 4) Cross-DB read isolation — schema USAGE is the gate
if [ -n "$KESTRA_PASSWORD" ]; then
    gxread=$(docker exec -e PGPASSWORD="$KESTRA_PASSWORD" "$CONTAINER" \
        psql -U kestra -d georag -tAc 'SELECT count(*) FROM silver.workspaces' 2>&1)
    case "$gxread" in
        *permission*denied*) check "kestra cannot read georag schemas (no USAGE granted)" ok ;;
        [0-9]*)              check "cross-DB read isolation" fail "kestra CAN read silver.workspaces" ;;
        *)                   check "cross-DB read isolation" fail "unexpected: $(echo "$gxread" | head -1)" ;;
    esac
else
    check "cross-DB read isolation" fail "skipped — no password"
fi

# 5) No grants in georag schemas
leaked=$(q "
    SELECT count(*) FROM information_schema.role_table_grants
     WHERE grantee = 'kestra'
       AND table_schema IN ('public','bronze','silver','gold','public_geoscience',
                            'audit','usage','outbox','workflow','workspace');" \
    | tr -d ' ')
[ "$leaked" = "0" ] \
    && check "no table grants leaked into georag schemas" ok \
    || check "grant isolation" fail "got $leaked grants"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
