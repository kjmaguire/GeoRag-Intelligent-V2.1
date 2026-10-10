#!/usr/bin/env bash
# =============================================================================
# scripts/build_airgap_bundle.sh
#
# §11.8 — build a single tar.gz containing everything a customer needs
# to install GeoRAG on an air-gapped K3s node:
#   1. Saved docker images for every service in the chart
#      (images/*.tar, plus images/index.txt: tarball -> image reference)
#   2. The Helm chart (packaged as a .tgz)
#   3. install.sh — single-command installer
#   4. values-airgap.yaml — chart values pre-tuned for air-gap
#   5. README.md — operator instructions
#
# Usage:
#   bash scripts/build_airgap_bundle.sh [--version v1.0.0] [--out dist/]
#
# Output: dist/georag-airgap-<version>.tar.gz (typically 15-20 GB: the
# images and the chart. No model weights are bundled — the chart deploys no
# inference server; see airgap/README.md, "Model weights", for the Qwen3
# weights the embedding and reranker sidecars need.)
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
# Accepted so old command lines keep working, and does nothing: this used to
# bundle vLLM model weights, and the chart has deployed no inference server
# since 2026-07-30.
INCLUDE_MODEL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --out)     OUT_DIR="$2"; shift 2 ;;
        --include-model) INCLUDE_MODEL=true; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# --out is relative to the repo root unless it is absolute.
case "$OUT_DIR" in
    /*) OUT_ABS="$OUT_DIR" ;;
    *)  OUT_ABS="$REPO_ROOT/$OUT_DIR" ;;
esac
STAGE="$OUT_ABS/_stage_$VERSION"
TARBALL="$OUT_ABS/georag-airgap-$VERSION.tar.gz"

# The registry prefix values-airgap.yaml puts in front of every image, and the
# default of install.sh's --registry. Images are pulled from upstream, so it is
# stripped below.
AIRGAP_REGISTRY="registry.internal.local/georag"
# Chart.yaml pins kubeVersion >=1.27.0; with no cluster to ask, helm assumes
# 1.20 and refuses to render unless told otherwise.
KUBE_VERSION="${KUBE_VERSION:-1.30.0}"

echo "============================================================"
echo "  GeoRAG air-gap bundle builder"
echo "  version : $VERSION"
echo "  stage   : $STAGE"
echo "  output  : $TARBALL"
echo "============================================================"

# Placeholder values for every secret the chart marks `required`, so it
# renders. Only the image references are read from the render. Keep in step
# with the `required` lines in charts/georag/templates/secrets.yaml: a missing
# one makes the render below fail, and that now stops the build with helm's
# own message rather than producing an empty image list.
RENDER_SECRETS=(
    --set "secrets.postgresPassword=x"
    --set "secrets.pgAppPassword=x"
    --set "secrets.martinDbPassword=x"
    --set "secrets.redisPassword=x"
    --set "secrets.fastapiServiceKey=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    --set "secrets.laravelAppKey=base64:x"
    --set "secrets.reverbAppKey=x"
    --set "secrets.reverbAppSecret=x"
)

# Print the unique image references the chart renders with the given extra
# `helm template` arguments (values files), one per line, sorted.
# This used to discard helm's stderr, and passed no --kube-version, so helm
# refused to render and the failure was invisible: an empty image list, then a
# bundle with no images in it.
render_images() {
    local rendered
    if ! rendered=$(docker run --rm \
            -v "$REPO_ROOT:/work" -w /work alpine/helm:latest \
            template georag charts/georag/ --kube-version "$KUBE_VERSION" \
            "$@" "${RENDER_SECRETS[@]}" 2>&1); then
        echo "ERROR: helm template failed:" >&2
        printf '%s\n' "$rendered" | head -20 >&2
        return 1
    fi
    printf '%s\n' "$rendered" \
        | { grep -E '^\s*image:\s' || true; } \
        | awk '{print $2}' \
        | tr -d '"' \
        | sort -u
}

mkdir -p "$STAGE/images" "$STAGE/chart"

echo
echo "→ collecting image list from chart"
AIRGAP_OUT="$(render_images -f charts/georag/values-airgap.yaml)" || exit 1
# The same chart with its default values, which is where the digest pins live.
DEFAULT_OUT="$(render_images)" || exit 1
mapfile -t IMAGES <<<"$AIRGAP_OUT"
mapfile -t DEFAULTS <<<"$DEFAULT_OUT"
if [ "${#IMAGES[@]}" -eq 0 ] || [ -z "${IMAGES[0]}" ]; then
    echo "ERROR: the chart rendered no images." >&2
    exit 1
fi

# Strip the air-gap registry prefix; we want to pull from upstream
# registries, then re-tag during install.
UPSTREAM_IMAGES=()
for img in "${IMAGES[@]}"; do
    case "$img" in
        "$AIRGAP_REGISTRY"/*) UPSTREAM_IMAGES+=("${img#"$AIRGAP_REGISTRY"/}") ;;
        *)
            echo "ERROR: $img does not start with $AIRGAP_REGISTRY/ — values-airgap.yaml" >&2
            echo "       imageRegistry and AIRGAP_REGISTRY in this script have drifted." >&2
            exit 1
            ;;
    esac
done

# Digest pins. values.yaml pins some images by digest (tag@sha256:...), and an
# image that has been through `docker save` / `ctr import` does not keep its
# registry manifest digest, so a pod that asks for the digest cannot find it
# locally and kubelet tries to pull it. values-airgap.yaml therefore names such
# images by tag only, and this script pulls the DIGEST-pinned reference (the pin
# is verified here, at build time) and ships it under the tag-only name.
# The two must agree: an image pinned in values.yaml with no tag-only
# counterpart in the air-gap render means one of the files was bumped without
# the other, and the bundle would carry the old image.
declare -A PIN_FOR=()
for ref in "${DEFAULTS[@]}"; do
    case "$ref" in
        *@sha256:*) PIN_FOR["${ref%%@*}"]="$ref" ;;
    esac
done
for tagonly in "${!PIN_FOR[@]}"; do
    found=0
    for img in "${UPSTREAM_IMAGES[@]}"; do
        if [ "$img" = "$tagonly" ]; then
            found=1
        fi
    done
    if [ "$found" -eq 0 ]; then
        echo "ERROR: values.yaml pins ${PIN_FOR[$tagonly]}" >&2
        echo "       but the air-gap render has no image named $tagonly." >&2
        echo "       Update the matching tag in charts/georag/values-airgap.yaml." >&2
        exit 1
    fi
done
for img in "${UPSTREAM_IMAGES[@]}"; do
    case "$img" in
        *@*)
            echo "ERROR: air-gap image $img carries a digest; name it by tag only" >&2
            echo "       in values-airgap.yaml (see the digest-pin note in this script)." >&2
            exit 1
            ;;
    esac
done

echo "  → ${#UPSTREAM_IMAGES[@]} unique images:"
printf '    - %s\n' "${UPSTREAM_IMAGES[@]}"

echo
echo "→ pulling + saving images (this is the long step)"
# images/index.txt maps each tarball to the reference it carries. install.sh
# reads it instead of rebuilding names from file names, which cannot be done:
# `tr '/:' '__'` is not reversible (qdrant/qdrant:v1.19.1 -> qdrant_qdrant_v1.19.1
# -> qdrant/qdrant/v1.19:1).
INDEX="$STAGE/images/index.txt"
{
    echo "# tarball<TAB>image reference (what the chart pulls, minus the registry prefix)"
} > "$INDEX"
for img in "${UPSTREAM_IMAGES[@]}"; do
    safe=$(printf '%s' "$img" | tr '/:' '__')
    out="$STAGE/images/$safe.tar"
    pull_ref="${PIN_FOR[$img]:-$img}"
    if [ -f "$out" ]; then
        echo "  ✓ cached $img"
    else
        echo "  ↓ $pull_ref"
        docker pull "$pull_ref" >/dev/null || { echo "    FAIL: could not pull $pull_ref" >&2; exit 1; }
        if [ "$pull_ref" != "$img" ]; then
            # A digest-only pull leaves the image without a tag, and the
            # tarball would then carry no name at all.
            docker tag "$pull_ref" "$img"
        fi
        docker save -o "$out" "$img"
    fi
    printf '%s\t%s\n' "$safe.tar" "$img" >> "$INDEX"
done

echo
echo "→ packaging Helm chart"
docker run --rm -v "$REPO_ROOT:/work" -v "$STAGE/chart:/out" -w /work alpine/helm:latest \
    package charts/georag/ --destination /out \
    >/dev/null
ls "$STAGE/chart/"

echo
echo "→ copying airgap support files"
cp "$REPO_ROOT/airgap/install.sh" "$STAGE/install.sh"
# The README says `./install.sh`; cp keeps the checkout's mode, which is 0644.
chmod 755 "$STAGE/install.sh"
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
        echo "pinned:"
        for img in "${UPSTREAM_IMAGES[@]}"; do
            if [ -n "${PIN_FOR[$img]:-}" ]; then
                echo "  - ${PIN_FOR[$img]}"
            fi
        done
        echo "chart:"
        for f in chart/*.tgz; do echo "  - $f"; done
        echo "files:"
        find . -type f ! -name MANIFEST.yaml | sort | while read -r f; do
            sha=$(sha256sum "$f" | awk '{print $1}')
            echo "  - { path: $f, sha256: $sha }"
        done
    } > MANIFEST.yaml
)

echo
echo "→ creating final tarball"
tar -C "$OUT_ABS" -czf "$TARBALL" "_stage_$VERSION"
size_mb=$(du -m "$TARBALL" | awk '{print $1}')
echo "  $TARBALL ($size_mb MB)"

echo
echo "→ cleaning stage"
rm -rf "$STAGE"

echo
echo "Bundle ready. Verify before shipping:"
echo "  bash scripts/verify_airgap_bundle.sh $TARBALL"
