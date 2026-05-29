#!/usr/bin/env bash
# =============================================================================
# scripts/section11_v2_acceptance.sh
#
# Master-plan §11.6/7/8 — Helm + K8s + air-gap deployment surface.
# Mirrors section11_acceptance.sh shape + exit-code semantics.
#
# Covers:
#   §11.6  — chart skeleton present, helm lint passes, all 3 values
#            files template without error
#   §11.7  — pre-rendered manifests present and match the chart output
#            (drift detector — fails if `regenerate_k8s_manifests.sh`
#            wasn't re-run after editing the chart)
#   §11.8  — air-gap scripts present + bash-valid, install.sh stub
#            checks pass
#
# What this harness does NOT do (deferred until a real K3s test env):
#   - actual `helm install` against a live cluster
#   - actual air-gap bundle build (takes ~30 min + 25GB disk)
#   - actual install.sh execution on an air-gapped node
#
# Pre-requisites:
#   - Docker available (we use alpine/helm image)
#
# Exit code 0 = §11-v2 surface green. 1 = at least one check failed.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=0
FAILED=()

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELM_IMAGE="${HELM_IMAGE:-alpine/helm:latest}"

check_file() {
    local f="$1"
    TOTAL=$((TOTAL + 1))
    if [ -f "$REPO_ROOT/$f" ]; then
        echo "  [PASS] $f present"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $f missing"
        FAILED+=("$f missing")
    fi
}

check_bash_valid() {
    local f="$1"
    TOTAL=$((TOTAL + 1))
    if [ -f "$REPO_ROOT/$f" ] && bash -n "$REPO_ROOT/$f" 2>/dev/null; then
        echo "  [PASS] $f syntactically valid"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $f has syntax errors"
        FAILED+=("$f syntax")
    fi
}

helm_run() {
    MSYS_NO_PATHCONV=1 docker run --rm \
        -v "$REPO_ROOT:/work" -w /work \
        "$HELM_IMAGE" "$@"
}

echo
echo "=============================================================="
echo "  Master-plan §11-v2 acceptance harness (Helm + K8s + air-gap)"
echo "=============================================================="
echo

# ----------------------------------------------------------------------------
# 1. §11.6 — chart skeleton
# ----------------------------------------------------------------------------
echo "-- §11.6 chart skeleton --"
check_file "charts/georag/Chart.yaml"
check_file "charts/georag/values.yaml"
check_file "charts/georag/values-k3s.yaml"
check_file "charts/georag/values-vanilla.yaml"
check_file "charts/georag/values-airgap.yaml"
check_file "charts/georag/README.md"
check_file "charts/georag/templates/_helpers.tpl"

# ----------------------------------------------------------------------------
# 2. §11.6 — every core service has a template
# ----------------------------------------------------------------------------
echo
echo "-- §11.6 service templates --"
for svc in postgresql pgbouncer neo4j qdrant redis seaweedfs fastapi laravel vllm hatchet martin dagster ingress jobs namespace secrets; do
    check_file "charts/georag/templates/$svc.yaml"
done

# ----------------------------------------------------------------------------
# 3. §11.6 — helm lint
# ----------------------------------------------------------------------------
echo
echo "-- §11.6 helm lint --"
TOTAL=$((TOTAL + 1))
if helm_run lint charts/georag/ >/tmp/helm-lint.log 2>&1; then
    echo "  [PASS] helm lint"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] helm lint failed:"
    cat /tmp/helm-lint.log | head -20 | sed 's/^/    /'
    FAILED+=("helm lint")
fi

# ----------------------------------------------------------------------------
# 4. §11.6 — helm template (all 3 flavors)
# ----------------------------------------------------------------------------
echo
echo "-- §11.6 helm template (all 3 values files) --"
SECRETS_ARGS=(
    --set "secrets.postgresPassword=x"
    --set "secrets.pgAppPassword=x"
    --set "secrets.neo4jPassword=neo4j/x"
    --set "secrets.redisPassword=x"
    --set "secrets.fastapiServiceKey=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    --set "secrets.laravelAppKey=base64:x"
)
for flavor in k3s vanilla airgap; do
    TOTAL=$((TOTAL + 1))
    if helm_run template georag charts/georag/ \
        -f "charts/georag/values-$flavor.yaml" \
        "${SECRETS_ARGS[@]}" >/tmp/helm-tmpl-$flavor.log 2>&1; then
        count=$(grep -cE "^kind:" /tmp/helm-tmpl-$flavor.log || true)
        echo "  [PASS] values-$flavor.yaml ($count resources)"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] values-$flavor.yaml failed to template:"
        head -15 /tmp/helm-tmpl-$flavor.log | sed 's/^/    /'
        FAILED+=("template $flavor")
    fi
done

# ----------------------------------------------------------------------------
# 5. §11.7 — pre-rendered manifests + drift detector
# ----------------------------------------------------------------------------
echo
echo "-- §11.7 pre-rendered manifests --"
check_file "kubernetes/manifests/k3s.yaml"
check_file "kubernetes/manifests/vanilla.yaml"
check_file "kubernetes/manifests/airgap.yaml"
check_file "kubernetes/manifests/README.md"
check_file "scripts/regenerate_k8s_manifests.sh"
check_bash_valid "scripts/regenerate_k8s_manifests.sh"

# Drift check: regenerate to /tmp and diff
echo
echo "-- §11.7 manifest drift detector --"
DRIFT_FOUND=0
SECRETS_ARGS_DRIFT=(
    --set "secrets.postgresPassword=CHANGEME"
    --set "secrets.pgAppPassword=CHANGEME"
    --set "secrets.neo4jPassword=neo4j/CHANGEME"
    --set "secrets.redisPassword=CHANGEME"
    --set "secrets.fastapiServiceKey=CHANGEME-rotate-this-key-to-32plus-chars-from-prod-secret"
    --set "secrets.laravelAppKey=base64:CHANGEME"
)
for flavor in k3s vanilla airgap; do
    TOTAL=$((TOTAL + 1))
    fresh=$(helm_run template georag charts/georag/ \
        -f "charts/georag/values-$flavor.yaml" \
        "${SECRETS_ARGS_DRIFT[@]}" 2>/dev/null)
    committed=$(cat "$REPO_ROOT/kubernetes/manifests/$flavor.yaml")
    if [ "$fresh" = "$committed" ]; then
        echo "  [PASS] $flavor.yaml in sync with chart"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $flavor.yaml has drifted — re-run scripts/regenerate_k8s_manifests.sh"
        FAILED+=("$flavor drift")
        DRIFT_FOUND=1
    fi
done

# ----------------------------------------------------------------------------
# 6. §11.7 — deployment docs
# ----------------------------------------------------------------------------
echo
echo "-- §11.7 deployment docs --"
check_file "docs/deployment/k8s-reference.md"
check_file "docs/deployment/k3s-quickstart.md"

# ----------------------------------------------------------------------------
# 7. §11.8 — air-gap scripts + airgap dir
# ----------------------------------------------------------------------------
echo
echo "-- §11.8 air-gap scripts --"
check_file "scripts/build_airgap_bundle.sh"
check_file "scripts/verify_airgap_bundle.sh"
check_file "airgap/install.sh"
check_file "airgap/README.md"
check_bash_valid "scripts/build_airgap_bundle.sh"
check_bash_valid "scripts/verify_airgap_bundle.sh"
check_bash_valid "airgap/install.sh"

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
echo
echo "=============================================================="
echo "  §11-v2 acceptance: $PASS / $TOTAL checks passed"
if [ ${#FAILED[@]} -ne 0 ]; then
    echo "  Failures:"
    for f in "${FAILED[@]}"; do
        echo "    - $f"
    done
    echo "=============================================================="
    exit 1
fi
echo "  §11-v2 surface green. Live K3s install test deferred until"
echo "  a real cluster is available (Kyle decision: 'ship chart +"
echo "  manifests now, defer install test')."
echo "=============================================================="
exit 0
