#!/usr/bin/env bash
# =============================================================================
# scripts/phase9_step3_verify.sh
#
# Phase 9 Step 3 done-definition — ACME wiring scaffold (R-P8-2).
#
#   1. Caddyfile global block carries `email {$CADDY_ACME_EMAIL:...}`
#   2. docker-compose.yml maps CADDY_ACME_EMAIL on the caddy service
#   3. Default env (placeholder email) — Caddy boots + HTTPS still 200
#   4. Email override via env propagates into the running container's
#      env (CADDY_ACME_EMAIL=phase9-test@local.example)
#   5. Caddyfile validates cleanly with the override
#   6. Runbook removed the "edit Caddyfile" step (now env-only)
#   7. Phase 8 Step 3 verifier still passes (regression)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
CADDYFILE="$REPO/caddy/Caddyfile"
COMPOSE="$REPO/docker-compose.yml"
RUNBOOK="$REPO/docs/runbooks/caddy_tls.md"
HTTPS_URL="${HTTPS_URL:-https://localhost:8443}"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cleanup() {
    # Restore default container env if we mutated it. The default
    # env from .env or compose is restored by recreating without
    # the override.
    if [ "${TOUCHED_CONTAINER:-0}" = "1" ]; then
        (cd "$REPO" && docker compose --profile dev-data --profile dev-light \
            up -d --force-recreate --no-deps caddy >/dev/null 2>&1) || true
    fi
}
trap cleanup EXIT

cat <<'BANNER'

============================================================
PHASE 9 STEP 3 — ACME WIRING SCAFFOLD VERIFICATION
============================================================
BANNER

# 1) Caddyfile email directive
if grep -q 'email {$CADDY_ACME_EMAIL:' "$CADDYFILE"; then
    check "Caddyfile global block uses email {\$CADDY_ACME_EMAIL:...} placeholder" ok
else
    check "caddyfile email" fail "directive missing"
fi

# 2) Compose env mapping
if grep -q 'CADDY_ACME_EMAIL: ${CADDY_ACME_EMAIL:-' "$COMPOSE"; then
    check "docker-compose.yml maps CADDY_ACME_EMAIL on caddy service" ok
else
    check "compose env" fail "CADDY_ACME_EMAIL mapping missing"
fi

# 3) Default boot
hz=$(curl -sk -o /dev/null -w '%{http_code}' "$HTTPS_URL/healthz")
[ "$hz" = "200" ] \
    && check "Default placeholder email → Caddy still boots + HTTPS 200" ok \
    || check "default boot" fail "got $hz"

# 4) Override propagates
TOUCHED_CONTAINER=1
docker exec georag-caddy printenv CADDY_ACME_EMAIL >/dev/null 2>&1
# Recreate with an override
(cd "$REPO" && CADDY_ACME_EMAIL='phase9-test@local.example' \
    docker compose --profile dev-data --profile dev-light \
    up -d --force-recreate --no-deps caddy >/dev/null 2>&1)
sleep 4
inside_env=$(docker exec georag-caddy printenv CADDY_ACME_EMAIL 2>/dev/null)
[ "$inside_env" = "phase9-test@local.example" ] \
    && check "CADDY_ACME_EMAIL override propagates into the container env" ok \
    || check "env override" fail "got '$inside_env'"

# 5) Validate with override
valid=$(docker exec georag-caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1)
echo "$valid" | grep -q 'Valid configuration' \
    && check "Caddyfile validates with the override active" ok \
    || check "caddy validate override" fail "$(echo "$valid" | tail -1)"

# 6) Runbook simplified
if grep -q 'CADDY_ACME_EMAIL=' "$RUNBOOK" \
    && ! grep -q '^acme_email ops@example.com' "$RUNBOOK"; then
    check "Runbook documents env-only swap (no Caddyfile edit step)" ok
else
    check "runbook simplified" fail "still references inline Caddyfile edit OR missing env"
fi

# 7) Phase 8 Step 3 regression — keeps default behaviour intact
# (the prior recreate restored placeholder default). Wait for restart.
sleep 4
p8s3=$(bash "$REPO/scripts/phase8_step3_verify.sh" 2>&1 | grep -E '^Result: ' | tail -1)
case "$p8s3" in
    'Result: 6 / 6 checks passed')
        check "Phase 8 Step 3 verifier still passes 6/6 (TLS posture preserved)" ok ;;
    *) check "phase8_step3 regression" fail "$p8s3" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
