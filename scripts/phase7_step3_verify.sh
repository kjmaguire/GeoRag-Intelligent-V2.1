#!/usr/bin/env bash
# =============================================================================
# scripts/phase7_step3_verify.sh
#
# Phase 7 Step 3 done-definition — TLS on the Caddy edge (R-P6-3).
#
#   1. Caddy container running + healthy
#   2. Caddyfile validates ('caddy validate')
#   3. HTTP :8087 healthz → 200 (back-compat)
#   4. HTTPS :8443 healthz → 200 (TLS works)
#   5. Cert chain on :8443 issued by Caddy's internal CA ("Caddy Local
#      Authority - 20XX" issuer DN)
#   6. caddy_data volume mounted (CA persisted across container life)
#   7. forward_auth still enforces auth on :8443 (no-auth GET → non-2xx)
#   8. Phase 6 Step 2 verifier (HTTP path) still passes — full regression
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
REPO="${REPO:-/home/georag/projects/georag}"
HTTP_URL="${HTTP_URL:-http://localhost:8087}"
HTTPS_URL="${HTTPS_URL:-https://localhost:8443}"

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
PHASE 7 STEP 3 — CADDY TLS LISTENER VERIFICATION
============================================================
BANNER

# 1) Container running
status=$(docker inspect -f '{{.State.Status}}' georag-caddy 2>/dev/null)
[ "$status" = "running" ] \
    && check "georag-caddy container running" ok \
    || check "container" fail "status=$status"

# 2) Caddyfile validate
valid=$(docker exec georag-caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1)
echo "$valid" | grep -q 'Valid configuration' \
    && check "Caddyfile passes 'caddy validate'" ok \
    || check "caddyfile validate" fail "$(echo "$valid" | tail -1)"

# 3) HTTP listener (regression of Phase 6 Step 2 path)
hz_http=$(curl -s -o /dev/null -w '%{http_code}' "$HTTP_URL/healthz")
[ "$hz_http" = "200" ] \
    && check "HTTP :8087 /healthz returns 200 (Phase 6 path intact)" ok \
    || check "http healthz" fail "got $hz_http"

# 4) HTTPS listener
hz_https=$(curl -sk -o /dev/null -w '%{http_code}' "$HTTPS_URL/healthz")
[ "$hz_https" = "200" ] \
    && check "HTTPS :8443 /healthz returns 200 (TLS handshake + healthz)" ok \
    || check "https healthz" fail "got $hz_https"

# 5) Internal-CA cert chain
cert_issuer=$(echo | openssl s_client -connect localhost:8443 \
    -servername localhost -showcerts 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null)
echo "$cert_issuer" | grep -qi 'Caddy Local Authority' \
    && check "TLS cert issued by Caddy internal CA ($cert_issuer)" ok \
    || check "cert issuer" fail "got: $cert_issuer"

# 6) caddy_data volume mount (persisted CA)
vol_present=$(docker inspect georag-caddy \
    --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
case "$vol_present" in
    *caddy_data*) check "caddy_data volume mounted at /data (CA persists)" ok ;;
    *) check "data volume" fail "got mount=$vol_present" ;;
esac

# 7) forward_auth still enforces auth on HTTPS path
auth_code=$(curl -sk -o /dev/null -w '%{http_code}' \
    "$HTTPS_URL/api/v1/main/flows/search?namespace=")
case "$auth_code" in
    2*) check "https forward_auth" fail "got 2xx ($auth_code) — auth not enforced" ;;
    *)  check "HTTPS /api/* rejected without auth (got $auth_code)" ok ;;
esac

# 8) Phase 6 Step 2 still passes — full regression
p6s2=$(bash "$REPO/scripts/phase6_step2_verify.sh" 2>&1 | grep -E '^Result: ' | tail -1)
case "$p6s2" in
    'Result: 8 / 8 checks passed')
        check "Phase 6 Step 2 (HTTP path + Sanctum auth) still passes 8/8" ok ;;
    *) check "phase6_step2 regression" fail "$p6s2" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
