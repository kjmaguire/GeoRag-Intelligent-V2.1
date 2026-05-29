#!/usr/bin/env bash
# =============================================================================
# airgap/install.sh
#
# §11.8 — single-command installer for an air-gapped K3s host.
# Ships inside the air-gap tarball; the operator unpacks the tarball
# and runs this from the unpacked directory.
#
# What it does:
#   1. Detect K3s containerd vs Docker as the runtime
#   2. Load every image in images/*.tar
#   3. Re-tag each image with the configured private-registry prefix
#      (registry.internal.local/georag/ by default; override via
#      `--registry`)
#   4. Install the chart via helm (uses the bundled .tgz, no internet)
#   5. Print a status summary
#
# Usage:
#   ./install.sh [--namespace georag] [--registry my.registry.local/georag]
#                [--secrets-file path/to/secrets.env]
# =============================================================================

set -euo pipefail

NAMESPACE="georag"
REGISTRY="registry.internal.local/georag"
SECRETS_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --namespace) NAMESPACE="$2"; shift 2 ;;
        --registry)  REGISTRY="$2"; shift 2 ;;
        --secrets-file) SECRETS_FILE="$2"; shift 2 ;;
        --help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -40
            exit 0
            ;;
        *) echo "Unknown arg: $1 (try --help)"; exit 1 ;;
    esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "============================================================"
echo "  GeoRAG air-gap install"
echo "  namespace : $NAMESPACE"
echo "  registry  : $REGISTRY"
echo "============================================================"

# ─── 1. Detect runtime ───────────────────────────────────────────────
RUNTIME=""
if command -v k3s >/dev/null 2>&1; then
    RUNTIME="k3s"
    echo "  runtime   : K3s containerd"
elif command -v docker >/dev/null 2>&1; then
    RUNTIME="docker"
    echo "  runtime   : Docker"
else
    echo "ERROR: no K3s or Docker found. Install one before continuing."
    exit 1
fi

# ─── 2. Load + re-tag images ─────────────────────────────────────────
echo
echo "→ loading images into local runtime"
for tar in images/*.tar; do
    [ -f "$tar" ] || continue
    name=$(basename "$tar" .tar | tr '_' '/' | sed 's/\.\([^.]*\)$/:\1/')
    echo "  ↓ $tar → $name"

    if [ "$RUNTIME" = "k3s" ]; then
        sudo k3s ctr image import "$tar"
        # Re-tag with the configured registry prefix so the chart's
        # `imageRegistry: registry.internal.local/georag` resolves.
        sudo k3s ctr image tag "$name" "$REGISTRY/$name"
    else
        docker load -i "$tar"
        docker tag "$name" "$REGISTRY/$name"
    fi
done

# ─── 3. Install Helm chart ───────────────────────────────────────────
echo
echo "→ installing chart"
CHART=$(ls chart/*.tgz | head -1)
if [ -z "$CHART" ]; then
    echo "ERROR: no chart .tgz found in chart/"
    exit 1
fi
echo "  chart: $CHART"

HELM_ARGS=(
    install georag "$CHART"
    -f values-airgap.yaml
    --create-namespace --namespace "$NAMESPACE"
    --set "global.imageRegistry=$REGISTRY"
)

if [ -n "$SECRETS_FILE" ] && [ -f "$SECRETS_FILE" ]; then
    echo "  → loading secrets from $SECRETS_FILE"
    # Expected format: KEY=VALUE per line (POSTGRES_PASSWORD=..., etc)
    # Convert to --set secrets.X=Y
    while IFS='=' read -r k v; do
        [ -z "$k" ] && continue
        # Snake-case → camelCase mapping
        case "$k" in
            POSTGRES_PASSWORD)   HELM_ARGS+=(--set "secrets.postgresPassword=$v") ;;
            PG_APP_PASSWORD)     HELM_ARGS+=(--set "secrets.pgAppPassword=$v") ;;
            NEO4J_PASSWORD)      HELM_ARGS+=(--set "secrets.neo4jPassword=$v") ;;
            REDIS_PASSWORD)      HELM_ARGS+=(--set "secrets.redisPassword=$v") ;;
            FASTAPI_SERVICE_KEY) HELM_ARGS+=(--set "secrets.fastapiServiceKey=$v") ;;
            LARAVEL_APP_KEY)     HELM_ARGS+=(--set "secrets.laravelAppKey=$v") ;;
        esac
    done < "$SECRETS_FILE"
else
    echo "  → minting random secrets (override via --secrets-file)"
    HELM_ARGS+=(
        --set "secrets.postgresPassword=$(openssl rand -base64 32)"
        --set "secrets.pgAppPassword=$(openssl rand -base64 32)"
        --set "secrets.neo4jPassword=neo4j/$(openssl rand -base64 24 | tr -d '/+=')"
        --set "secrets.redisPassword=$(openssl rand -base64 32)"
        --set "secrets.fastapiServiceKey=$(openssl rand -base64 48)"
        --set "secrets.laravelAppKey=base64:$(openssl rand -base64 32)"
    )
fi

helm "${HELM_ARGS[@]}"

# ─── 4. Status ───────────────────────────────────────────────────────
echo
echo "→ waiting for pods to roll out (max 5 min)"
kubectl -n "$NAMESPACE" rollout status statefulset/georag-postgresql --timeout=300s || true
kubectl -n "$NAMESPACE" rollout status deployment/georag-fastapi --timeout=300s || true

echo
echo "→ status"
kubectl -n "$NAMESPACE" get pods

echo
echo "============================================================"
echo "  Install complete. Ingress: kubectl -n $NAMESPACE get ingress"
echo "============================================================"
