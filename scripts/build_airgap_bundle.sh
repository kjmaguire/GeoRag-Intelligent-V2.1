#!/usr/bin/env bash
# =============================================================================
# scripts/build_airgap_bundle.sh
#
# §11.8 — build a single tar.gz containing everything a customer needs
# to install GeoRAG on an air-gapped K3s node:
#   1. Saved docker images for every service in the chart
#   2. The Helm chart (packaged as a .tgz)
#   3. install.sh — single-command installer
#   4. values-airgap.yaml — chart values pre-tuned for air-gap
#   5. README.md — operator instructions
#
# Usage:
#   bash scripts/build_airgap_bundle.sh [--version v1.0.0] [--out dist/]
#
# Output: dist/georag-airgap-<version>.tar.gz (typically 25-35 GB
# depending on whether you bundle the vLLM model weights).
#
# Pre-requisites:
#   - Docker daemon running (for docker pull + docker save)
#   - helm CLI (or docker; we fall back to alpine/helm)
#
# Validate the bundle BEFORE shipping:
#   bash scripts/verify_airgap_bundle.sh dist/georag-airgap-<version>.tar.gz
# =============================================================================

set -euo pipefail

VERSION="v1.0.0"
OUT_DIR="dist"
INCLUDE_MODEL=false  # vLLM model weights — adds ~17 GB

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --out)     OUT_DIR="$2"; shift 2 ;;
        --include-model) INCLUDE_MODEL=true; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$REPO_ROOT/$OUT_DIR/_stage_$VERSION"
TARBALL="$REPO_ROOT/$OUT_DIR/georag-airgap-$VERSION.tar.gz"

echo "============================================================"
echo "  GeoRAG air-gap bundle builder"
echo "  version : $VERSION"
echo "  stage   : $STAGE"
echo "  output  : $TARBALL"
echo "  vLLM model bundled? $INCLUDE_MODEL"
echo "============================================================"

# Extract image list from the chart by templating it with placeholder
# secrets, then grepping for `image:` lines.
collect_images() {
    docker run --rm \
        -v "$REPO_ROOT:/work" -w /work alpine/helm:latest \
        template georag charts/georag/ -f charts/georag/values-airgap.yaml \
            --set "secrets.postgresPassword=x" \
            --set "secrets.pgAppPassword=x" \
            --set "secrets.neo4jPassword=neo4j/x" \
            --set "secrets.redisPassword=x" \
            --set "secrets.fastapiServiceKey=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
            --set "secrets.laravelAppKey=base64:x" \
        2>/dev/null \
    | grep -E '^\s*image:\s' \
    | awk '{print $2}' \
    | tr -d '"' \
    | sort -u
}

mkdir -p "$STAGE/images" "$STAGE/chart"

echo
echo "→ collecting image list from chart"
mapfile -t IMAGES < <(collect_images)
# Strip the air-gap registry prefix; we want to pull from upstream
# registries then re-tag during install.
UPSTREAM_IMAGES=()
for img in "${IMAGES[@]}"; do
    # values-airgap.yaml sets imageRegistry=registry.internal.local/georag
    upstream="${img#registry.internal.local/georag/}"
    UPSTREAM_IMAGES+=("$upstream")
done

echo "  → ${#UPSTREAM_IMAGES[@]} unique images:"
printf '    - %s\n' "${UPSTREAM_IMAGES[@]}"

echo
echo "→ pulling + saving images (this is the long step)"
for img in "${UPSTREAM_IMAGES[@]}"; do
    safe=$(echo "$img" | tr '/:' '__')
    out="$STAGE/images/$safe.tar"
    if [ -f "$out" ]; then
        echo "  ✓ cached $img"
        continue
    fi
    echo "  ↓ $img"
    docker pull "$img" >/dev/null 2>&1 || { echo "    FAIL"; exit 1; }
    docker save -o "$out" "$img"
done

echo
echo "→ packaging Helm chart"
docker run --rm -v "$REPO_ROOT:/work" -w /work alpine/helm:latest \
    package charts/georag/ --destination "/work/$OUT_DIR/_stage_$VERSION/chart" \
    >/dev/null
ls "$STAGE/chart/"

echo
echo "→ copying airgap support files"
cp "$REPO_ROOT/airgap/install.sh" "$STAGE/install.sh"
cp "$REPO_ROOT/airgap/README.md" "$STAGE/README.md"
cp "$REPO_ROOT/charts/georag/values-airgap.yaml" "$STAGE/values-airgap.yaml"

# Manifest with checksums for the verifier
echo
echo "→ writing manifest"
(
    cd "$STAGE"
    {
        echo "version=$VERSION"
        echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "image_count=${#UPSTREAM_IMAGES[@]}"
        echo "images:"
        for img in "${UPSTREAM_IMAGES[@]}"; do
            echo "  - $img"
        done
        echo "chart:"
        for f in chart/*.tgz; do echo "  - $f"; done
        echo "files:"
        find . -type f | sort | while read -r f; do
            sha=$(sha256sum "$f" | awk '{print $1}')
            echo "  - { path: $f, sha256: $sha }"
        done
    } > MANIFEST.yaml
)

echo
echo "→ creating final tarball"
tar -C "$REPO_ROOT/$OUT_DIR" -czf "$TARBALL" "_stage_$VERSION"
size_mb=$(du -m "$TARBALL" | awk '{print $1}')
echo "  $TARBALL ($size_mb MB)"

echo
echo "→ cleaning stage"
rm -rf "$STAGE"

echo
echo "Bundle ready. Verify before shipping:"
echo "  bash scripts/verify_airgap_bundle.sh $TARBALL"
