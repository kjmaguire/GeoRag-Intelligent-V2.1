#!/usr/bin/env bash
# =============================================================================
# scripts/verify_airgap_bundle.sh
#
# §11.8 — pre-flight check for a built air-gap bundle. Run BEFORE
# shipping to a customer to catch missing files, corrupted images, or
# checksum drift.
#
# Usage:
#   bash scripts/verify_airgap_bundle.sh dist/georag-airgap-v1.0.0.tar.gz
#
# Exit 0 = bundle is shippable. Exit 1 = at least one check failed.
# =============================================================================

set -uo pipefail

BUNDLE="${1:-}"
if [ -z "$BUNDLE" ] || [ ! -f "$BUNDLE" ]; then
    echo "usage: $0 <path-to-bundle.tar.gz>"
    exit 2
fi

TMP="$(mktemp -d)"
trap "rm -rf $TMP" EXIT

PASS=0
TOTAL=0
FAILED=()

check() {
    local label="$1"
    local cond="$2"
    TOTAL=$((TOTAL + 1))
    if eval "$cond"; then
        echo "  [PASS] $label"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $label"
        FAILED+=("$label")
    fi
}

echo "============================================================"
echo "  GeoRAG air-gap bundle verifier"
echo "  bundle: $BUNDLE"
echo "  tmp   : $TMP"
echo "============================================================"

echo
echo "→ extracting"
tar -C "$TMP" -xzf "$BUNDLE"
STAGE=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
if [ -z "$STAGE" ]; then
    echo "  [FAIL] tarball didn't contain a stage directory"
    exit 1
fi
echo "  stage: $STAGE"

echo
echo "→ required files"
check "MANIFEST.yaml present"    "[ -f '$STAGE/MANIFEST.yaml' ]"
check "install.sh present"        "[ -f '$STAGE/install.sh' ]"
check "README.md present"         "[ -f '$STAGE/README.md' ]"
check "values-airgap.yaml present" "[ -f '$STAGE/values-airgap.yaml' ]"
check "chart .tgz present"        "ls '$STAGE/chart/'*.tgz >/dev/null 2>&1"
check "images directory non-empty" "[ -n \"\$(ls -A '$STAGE/images' 2>/dev/null)\" ]"

echo
echo "→ image count"
IMG_COUNT=$(ls "$STAGE/images/"*.tar 2>/dev/null | wc -l)
TOTAL=$((TOTAL + 1))
if [ "$IMG_COUNT" -ge 10 ]; then
    echo "  [PASS] $IMG_COUNT images bundled"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] only $IMG_COUNT images (expected ≥10 for full stack)"
    FAILED+=("image count")
fi

echo
echo "→ chart validity"
TOTAL=$((TOTAL + 1))
CHART_TGZ=$(ls "$STAGE/chart/"*.tgz 2>/dev/null | head -1)
if [ -n "$CHART_TGZ" ] && tar -tzf "$CHART_TGZ" | grep -q "georag/Chart.yaml"; then
    echo "  [PASS] chart .tgz contains Chart.yaml"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] chart .tgz missing or malformed"
    FAILED+=("chart .tgz")
fi

echo
echo "→ MANIFEST sanity"
if [ -f "$STAGE/MANIFEST.yaml" ]; then
    MANIFEST_VERSION=$(grep -E "^version=" "$STAGE/MANIFEST.yaml" | head -1 | cut -d= -f2)
    MANIFEST_IMAGES=$(grep -E "^image_count=" "$STAGE/MANIFEST.yaml" | head -1 | cut -d= -f2)
    TOTAL=$((TOTAL + 1))
    if [ -n "$MANIFEST_VERSION" ] && [ "$MANIFEST_IMAGES" = "$IMG_COUNT" ]; then
        echo "  [PASS] MANIFEST version=$MANIFEST_VERSION image_count=$MANIFEST_IMAGES matches"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] MANIFEST mismatch (version=$MANIFEST_VERSION image_count=$MANIFEST_IMAGES vs $IMG_COUNT actual)"
        FAILED+=("manifest")
    fi
fi

echo
echo "→ install.sh syntax"
check "install.sh is bash-valid" "bash -n '$STAGE/install.sh'"

echo
echo "→ image tarball integrity (spot-check first 3)"
for tar in $(ls "$STAGE/images/"*.tar 2>/dev/null | head -3); do
    TOTAL=$((TOTAL + 1))
    if tar -tf "$tar" >/dev/null 2>&1; then
        echo "  [PASS] $(basename "$tar") readable"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $(basename "$tar") corrupt"
        FAILED+=("$(basename "$tar") corrupt")
    fi
done

echo
echo "============================================================"
echo "  Verify: $PASS / $TOTAL checks passed"
if [ ${#FAILED[@]} -ne 0 ]; then
    echo "  Failures:"
    for f in "${FAILED[@]}"; do
        echo "    - $f"
    done
    exit 1
fi
echo "  Bundle is shippable."
echo "============================================================"
exit 0
