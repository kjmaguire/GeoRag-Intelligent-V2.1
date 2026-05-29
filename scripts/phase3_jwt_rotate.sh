#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_jwt_rotate.sh
#
# Phase 3 Step 3 helper — mint a per-flow JWT and (optionally) write it
# into Kestra's secret store. Used by the operator when:
#   - Provisioning a new Kestra flow that calls FastAPI's
#     /internal/v1/integrations/<flow>/trigger
#   - Rotating an existing flow's JWT (suspected leak, scheduled
#     hygiene)
#
# Usage:
#   phase3_jwt_rotate.sh mint <flow_name> [ttl_hours=24]
#       — print the JWT to stdout (operator pastes into Kestra UI)
#   phase3_jwt_rotate.sh write <flow_name> [ttl_hours=24]
#       — mint + write to Kestra's KV store as `flow_jwt_<flow_name>`
#         via Kestra's REST API (basic-auth from .env)
#   phase3_jwt_rotate.sh rotate <flow_name> [ttl_hours=24]
#       — alias for `write`
#
#   phase3_jwt_rotate.sh provision-key <flow_name> [kid=primary]
#       — Phase 5 Step 2: generate a per-flow signing secret and write
#         it into workflow.flow_registry (encrypted via pgcrypto). After
#         this, future mints of this flow's JWT use the per-flow key
#         and set the `kid` claim.
# =============================================================================

set -euo pipefail

ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KESTRA_PORT=$(awk -F= '/^KESTRA_PORT=/         { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
KESTRA_PORT="${KESTRA_PORT:-8086}"
KESTRA_USER=$(awk -F= '/^KESTRA_BASIC_AUTH_USER=/      { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
KESTRA_USER="${KESTRA_USER:-admin@georag.local}"
KESTRA_PASS=$(awk -F= '/^KESTRA_BASIC_AUTH_PASSWORD=/  { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"
KESTRA_NAMESPACE="${KESTRA_NAMESPACE:-georag}"

cmd="${1:-}"
flow="${2:-}"
ttl_hours="${3:-24}"

usage() {
    cat <<USAGE >&2
usage: $0 mint   <flow_name> [ttl_hours=24]   # print to stdout
       $0 write  <flow_name> [ttl_hours=24]   # write to Kestra KV
       $0 rotate <flow_name> [ttl_hours=24]   # alias for write
USAGE
    exit 2
}

[ -z "$cmd" ] && usage
[ -z "$flow" ] && usage

# Per-subcommand argument validation. `provision-key` uses $3 as a kid
# string (not a ttl) so it bypasses the numeric check.
if [ "$cmd" != "provision-key" ]; then
    [[ "$ttl_hours" =~ ^[0-9]+$ ]] || usage
    ttl_seconds=$((ttl_hours * 3600))
fi

# Mint via the fastapi container — that's where the signing secret lives.
# Skipped for provision-key (its own case block does the SQL).
if [ "$cmd" != "provision-key" ]; then
    JWT=$(docker exec "$FASTAPI_CONTAINER" python3 -c "
import sys; sys.path.insert(0, '/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('${flow}', ttl_seconds=${ttl_seconds}), end='')
")
else
    JWT=""
fi

if [ "$cmd" != "provision-key" ] && [ -z "$JWT" ]; then
    echo "  [FAIL] mint returned empty" >&2
    exit 1
fi

case "$cmd" in
    mint)
        echo "$JWT"
        ;;

    write|rotate)
        if [ -z "$KESTRA_PASS" ]; then
            echo "  [FAIL] KESTRA_BASIC_AUTH_PASSWORD not set in $ENVFILE" >&2
            exit 1
        fi
        # Kestra v1.x KV store API: PUT /api/v1/{tenant}/namespaces/{ns}/kv/{key}
        # community edition tenant is `main`. The KV value is a string;
        # Kestra YAML reads it via {{ kv('flow_jwt_<flow>') }}.
        key="flow_jwt_${flow}"
        resp=$(curl -s -u "${KESTRA_USER}:${KESTRA_PASS}" \
            -X PUT \
            -H 'Content-Type: text/plain' \
            -w '\n%{http_code}' \
            "http://localhost:${KESTRA_PORT}/api/v1/main/namespaces/${KESTRA_NAMESPACE}/kv/${key}" \
            --data-binary "${JWT}")
        body=$(echo "$resp" | head -n -1)
        code=$(echo "$resp" | tail -n 1)
        case "$code" in
            200|201|204)
                echo "OK — wrote ${key} to Kestra KV (namespace=${KESTRA_NAMESPACE}, ttl=${ttl_hours}h)"
                ;;
            *)
                echo "  [FAIL] Kestra KV PUT returned HTTP ${code}: ${body}" >&2
                exit 1
                ;;
        esac
        ;;

    provision-key)
        # Phase 5 Step 2 — generate a per-flow JWT signing secret.
        # Phase 6 Step 3 (R-P5-2) — accept an optional fourth arg
        # `overlap_hours`; non-zero means the prior kid stays valid
        # for that many hours after the new one lands, so in-flight
        # tokens don't suddenly start failing verify.
        ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
        ENC_KEY=$(grep '^AUDIT_ENCRYPTION_KEY=' "$ENVFILE" | cut -d= -f2- | head -1)
        if [ -z "$ENC_KEY" ]; then
            echo "AUDIT_ENCRYPTION_KEY not set in $ENVFILE" >&2
            exit 1
        fi
        kid="${3:-primary}"
        overlap_hours="${4:-0}"
        if ! [[ "$overlap_hours" =~ ^[0-9]+$ ]]; then
            echo "overlap_hours must be a non-negative integer, got '$overlap_hours'" >&2
            exit 1
        fi
        secret=$(openssl rand -hex 32)
        docker exec georag-postgresql psql -U georag -d georag -q -c "
            SELECT set_config('app.audit_encryption_key', '${ENC_KEY}', false);
            SELECT workflow.set_flow_jwt_secret(
                '${flow}', '${kid}', '${secret}', ${overlap_hours}
            );
        " >/dev/null
        echo "OK — per-flow JWT secret provisioned"
        echo "  flow_name      : ${flow}"
        echo "  kid            : ${kid}"
        echo "  secret         : ${secret}"
        echo "  overlap_hours  : ${overlap_hours}"
        echo
        if [ "$overlap_hours" = "0" ]; then
            echo "Prior kid (if any) was retired immediately; tokens minted under it will reject."
        else
            echo "Prior kid (if any) stays valid for ${overlap_hours}h; existing in-flight tokens keep verifying."
        fi
        echo "Future mints of '${flow}' will sign with this key + emit 'kid' header."
        ;;

    *)
        usage
        ;;
esac
