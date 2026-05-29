#!/usr/bin/env bash
# =============================================================================
# scripts/phase8_step3_verify.sh
#
# Phase 8 Step 3 done-definition — parametrized Caddy TLS issuer
# (R-P7-3).
#
#   1. Caddyfile references the {$CADDY_TLS_ISSUER:...} placeholder
#   2. docker-compose.yml sets CADDY_TLS_ISSUER on the caddy service
#   3. Default env (CADDY_TLS_ISSUER unset → 'internal') still
#      issues an internal-CA leaf
#   4. Caddyfile passes 'caddy validate'
#   5. docs/runbooks/caddy_tls.md exists + documents the swap
#   6. Phase 7 Step 3 still passes (regression — same TLS posture
#      via env default)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
CADDYFILE="$REPO/caddy/Caddyfile"
COMPOSE="$REPO/docker-compose.yml"
RUNBOOK="$REPO/docs/runbooks/caddy_tls.md"

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
PHASE 8 STEP 3 — CADDY TLS ISSUER PARAMETRIZATION
============================================================
BANNER

# 1) Caddyfile placeholder
if grep -q '{\$CADDY_TLS_ISSUER:internal}' "$CADDYFILE"; then
    check "Caddyfile references {\$CADDY_TLS_ISSUER:internal} placeholder" ok
else
    check "caddyfile env" fail "placeholder missing"
fi

# 2) compose env mapping
if grep -q 'CADDY_TLS_ISSUER: ${CADDY_TLS_ISSUER:-internal}' "$COMPOSE"; then
    check "docker-compose.yml maps CADDY_TLS_ISSUER on the caddy service" ok
else
    check "compose env" fail "CADDY_TLS_ISSUER mapping missing"
fi

# 3) Default still issues internal CA
issuer=$(echo | openssl s_client -connect localhost:8443 \
    -servername localhost 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null)
echo "$issuer" | grep -qi 'Caddy Local Authority' \
    && check "Default issuer = internal CA (issuer=$issuer)" ok \
    || check "issuer default" fail "got: $issuer"

# 4) Validate
valid=$(docker exec georag-caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1)
echo "$valid" | grep -q 'Valid configuration' \
    && check "Caddyfile passes 'caddy validate' with env-templated issuer" ok \
    || check "caddy validate" fail "$(echo "$valid" | tail -1)"

# 5) Runbook
if [ -s "$RUNBOOK" ] \
    && grep -q 'CADDY_TLS_ISSUER=acme' "$RUNBOOK" \
    && grep -q 'acme_email' "$RUNBOOK"; then
    check "docs/runbooks/caddy_tls.md documents the internal→acme swap" ok
else
    check "runbook" fail "missing or incomplete"
fi

# 6) Phase 7 Step 3 regression
p7s3=$(bash "$REPO/scripts/phase7_step3_verify.sh" 2>&1 | grep -E '^Result: ' | tail -1)
case "$p7s3" in
    'Result: 8 / 8 checks passed')
        check "Phase 7 Step 3 still passes 8/8 (TLS posture preserved)" ok ;;
    *) check "phase7_step3 regression" fail "$p7s3" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
