#!/usr/bin/env bash
# =============================================================================
# scripts/regenerate_k8s_manifests.sh
#
# Re-render kubernetes/manifests/{k3s,vanilla,airgap}.yaml from the
# chart at charts/georag/. Secrets are rendered as `CHANGEME` so the
# files are safe to commit; operators rotate before deploy (see
# kubernetes/manifests/README.md).
# =============================================================================

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
HELM_IMAGE="${HELM_IMAGE:-alpine/helm:latest}"
SETS=(
    --set "secrets.postgresPassword=CHANGEME"
    --set "secrets.pgAppPassword=CHANGEME"
    --set "secrets.neo4jPassword=neo4j/CHANGEME"
    --set "secrets.redisPassword=CHANGEME"
    --set "secrets.fastapiServiceKey=CHANGEME-rotate-this-key-to-32plus-chars-from-prod-secret"
    --set "secrets.laravelAppKey=base64:CHANGEME"
)

mkdir -p "$REPO_ROOT/kubernetes/manifests"

run_helm() {
    MSYS_NO_PATHCONV=1 docker run --rm \
        -v "$REPO_ROOT:/work" \
        -w /work \
        "$HELM_IMAGE" "$@"
}

for flavor in k3s vanilla airgap; do
    out="$REPO_ROOT/kubernetes/manifests/$flavor.yaml"
    echo "→ rendering $out"
    run_helm template georag charts/georag/ \
        -f "charts/georag/values-$flavor.yaml" \
        "${SETS[@]}" > "$out"
    count=$(grep -cE "^kind:" "$out" || true)
    echo "  $count resources"
done

echo
echo "Done. Review the diff:"
echo "  git diff kubernetes/manifests/"
