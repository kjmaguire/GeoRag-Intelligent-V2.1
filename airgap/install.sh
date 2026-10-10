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
#   2. Load every image in images/*.tar, naming each from images/index.txt
#   3. Re-tag each image with the configured private-registry prefix
#      (registry.internal.local/georag/ by default; override via
#      `--registry`). This tags on THIS node only; it does not push.
#   4. `helm upgrade --install` the bundled chart .tgz (no internet), so a
#      second run upgrades the release in place
#   5. Print a status summary
#
# Usage:
#   ./install.sh [--namespace georag] [--registry my.registry.local/georag]
#                [--secrets-file path/to/secrets.env] [--reverb-app-key KEY]
#
# Secrets (KEY=VALUE per line; the keys are listed in README.md):
#   --secrets-file F   use F. It must exist.
#   (no flag)          ./georag-secrets.env if it is there, else on a first
#                      install a new one is written (mode 600) with random
#                      values. KEEP IT: it is the only copy, and re-running
#                      against existing data volumes needs the same passwords.
#   On a first install any required key the file lacks is generated and
#   appended to it. On an existing release nothing is generated: the release's
#   own secrets are kept (`helm --reuse-values`) and the file only overrides.
#
# --reverb-app-key KEY  The browser's Reverb key. It is baked into the laravel
#                      image when that is built (VITE_REVERB_APP_KEY) and must
#                      match it; this installer cannot know it. Without the flag
#                      or a REVERB_APP_KEY line a random one is generated, and
#                      live chat then connects only if the image was built with
#                      that value (README.md, "Live chat").
#
# HELM_TIMEOUT (default 20m) bounds the wait for the pg-init and schema hooks;
# the schema Job runs every migration on a fresh database.
# =============================================================================

set -euo pipefail

NAMESPACE="georag"
# The chart names its objects <release>-<component>; the status commands below
# and the README use this one.
RELEASE="georag"
REGISTRY="registry.internal.local/georag"
SECRETS_FILE=""
REVERB_APP_KEY_ARG=""
DEFAULT_SECRETS_FILE="georag-secrets.env"

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --namespace) NAMESPACE="${2:?--namespace needs a value}"; shift 2 ;;
        --registry)  REGISTRY="${2:?--registry needs a value}"; shift 2 ;;
        --secrets-file) SECRETS_FILE="${2:?--secrets-file needs a value}"; shift 2 ;;
        --reverb-app-key) REVERB_APP_KEY_ARG="${2:?--reverb-app-key needs a value}"; shift 2 ;;
        --help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -60
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

command -v helm >/dev/null 2>&1 \
    || die "helm not found. It is a prerequisite (README.md); install it before loading ~20 GB of images."

# ─── 1. Detect runtime ───────────────────────────────────────────────
RUNTIME=""
if command -v k3s >/dev/null 2>&1; then
    RUNTIME="k3s"
    echo "  runtime   : K3s containerd"
elif command -v docker >/dev/null 2>&1; then
    RUNTIME="docker"
    echo "  runtime   : Docker"
else
    die "no K3s or Docker found. Install one before continuing."
fi
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

# containerd names an imported image by its docker.io-normalised reference
# (redis:8.6-alpine is docker.io/library/redis:8.6-alpine), and `ctr image tag`
# does not normalise its source, so the source has to be given in that form.
canonical_ref() {
    local ref="$1" first="${1%%/*}"
    if [[ "$ref" != */* ]]; then
        echo "docker.io/library/$ref"
    elif [[ "$first" == *.* || "$first" == *:* || "$first" == "localhost" ]]; then
        echo "$ref"
    else
        echo "docker.io/$ref"
    fi
}

# ─── 2. Load + re-tag images ─────────────────────────────────────────
# images/index.txt (tarball<TAB>reference) is written by
# scripts/build_airgap_bundle.sh. The reference cannot be rebuilt from the file
# name: the bundler turns '/' and ':' into '_', so qdrant/qdrant:v1.19.1 is
# qdrant_qdrant_v1.19.1.tar and no rule gets back to the name.
echo
echo "→ loading images into local runtime"
INDEX="images/index.txt"
[ -f "$INDEX" ] || die "$INDEX missing. This bundle predates it; rebuild it with scripts/build_airgap_bundle.sh."
loaded=0
while IFS=$'\t' read -r tarball ref || [ -n "$tarball" ]; do
    case "$tarball" in ''|'#'*) continue ;; esac
    [ -n "$ref" ] || die "$INDEX: no image reference for $tarball"
    [ -f "images/$tarball" ] || die "images/$tarball is listed in $INDEX but missing from the bundle"
    dst="$REGISTRY/$ref"
    echo "  ↓ images/$tarball → $dst"

    # </dev/null: nothing in here may read the index off the loop's stdin.
    if [ "$RUNTIME" = "k3s" ]; then
        $SUDO k3s ctr image import "images/$tarball" </dev/null
        src="$(canonical_ref "$ref")"
        if ! $SUDO k3s ctr image ls -q </dev/null | grep -Fxq "$src"; then
            die "imported images/$tarball but containerd has no image named $src (\`k3s ctr image ls\` shows what it has)"
        fi
        # --force: a re-run (an upgrade) replaces the tag.
        $SUDO k3s ctr image tag --force "$src" "$dst" </dev/null
    else
        docker load -i "images/$tarball" </dev/null
        docker tag "$ref" "$dst" </dev/null
    fi
    loaded=$((loaded + 1))
done < "$INDEX"
[ "$loaded" -gt 0 ] || die "$INDEX lists no images"

# ─── 3. Secrets ──────────────────────────────────────────────────────
echo
echo "→ secrets"
declare -A KEYMAP=(
    [POSTGRES_PASSWORD]=postgresPassword
    [PG_APP_PASSWORD]=pgAppPassword
    [MARTIN_DB_PASSWORD]=martinDbPassword
    [REDIS_PASSWORD]=redisPassword
    [FASTAPI_SERVICE_KEY]=fastapiServiceKey
    [LARAVEL_APP_KEY]=laravelAppKey
    [HATCHET_CLIENT_TOKEN]=hatchetClientToken
    [HATCHET_DB_PASSWORD]=hatchetDbPassword
    [QDRANT_API_KEY]=qdrantApiKey
    [COHERE_API_KEY]=cohereApiKey
    [REVERB_APP_ID]=reverbAppId
    [REVERB_APP_KEY]=reverbAppKey
    [REVERB_APP_SECRET]=reverbAppSecret
)
# What a first install generates when the file lacks it. HATCHET_CLIENT_TOKEN
# cannot be generated (the engine mints it after it is up; README.md, "Hatchet
# token"), and QDRANT_API_KEY / COHERE_API_KEY are optional.
MINTABLE=(
    POSTGRES_PASSWORD PG_APP_PASSWORD MARTIN_DB_PASSWORD REDIS_PASSWORD
    FASTAPI_SERVICE_KEY LARAVEL_APP_KEY HATCHET_DB_PASSWORD
    REVERB_APP_SECRET REVERB_APP_KEY
)
declare -A S=()

mint() {
    case "$1" in
        POSTGRES_PASSWORD|PG_APP_PASSWORD|REDIS_PASSWORD|REVERB_APP_SECRET) openssl rand -base64 32 ;;
        FASTAPI_SERVICE_KEY) openssl rand -base64 48 ;;
        LARAVEL_APP_KEY) echo "base64:$(openssl rand -base64 32)" ;;
        # Embedded in URLs (Martin's and the Hatchet engine's DATABASE_URL), so hex.
        MARTIN_DB_PASSWORD|HATCHET_DB_PASSWORD) openssl rand -hex 32 ;;
        REVERB_APP_KEY) openssl rand -hex 16 ;;
        *) die "internal error: no generator for $1" ;;
    esac
}

load_secrets_file() {
    local file="$1" line k v
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in ''|'#'*) continue ;; esac
        line="${line#export }"
        case "$line" in
            *=*) ;;
            *) echo "  ! $file: ignoring a line with no '='"; continue ;;
        esac
        k="${line%%=*}"
        v="${line#*=}"
        case "$v" in
            \"*\") v="${v#\"}"; v="${v%\"}" ;;
            \'*\') v="${v#\'}"; v="${v%\'}" ;;
        esac
        if [ -n "${KEYMAP[$k]:-}" ]; then
            S[$k]="$v"
        else
            echo "  ! $file: ignoring unknown key $k"
        fi
    done < "$file"
}

EXISTING=0
if helm status "$RELEASE" --namespace "$NAMESPACE" >/dev/null 2>&1; then
    EXISTING=1
fi

if [ -n "$SECRETS_FILE" ]; then
    [ -f "$SECRETS_FILE" ] || die "--secrets-file $SECRETS_FILE: no such file"
elif [ -f "$DEFAULT_SECRETS_FILE" ]; then
    SECRETS_FILE="$DEFAULT_SECRETS_FILE"
    echo "  → using ./$DEFAULT_SECRETS_FILE from an earlier run"
elif [ "$EXISTING" -eq 0 ]; then
    SECRETS_FILE="$DEFAULT_SECRETS_FILE"
    ( umask 077; : > "$SECRETS_FILE" )
fi
if [ -n "$SECRETS_FILE" ]; then
    load_secrets_file "$SECRETS_FILE"
fi
if [ -n "$REVERB_APP_KEY_ARG" ]; then
    S[REVERB_APP_KEY]="$REVERB_APP_KEY_ARG"
fi

MINTED=()
if [ "$EXISTING" -eq 0 ]; then
    for k in "${MINTABLE[@]}"; do
        if [ -z "${S[$k]:-}" ]; then
            S[$k]="$(mint "$k")"
            MINTED+=("$k")
            # Appended to the file so a re-run, and every upgrade, uses the same
            # values. (A flag-supplied REVERB_APP_KEY is not "minted": see above.)
            printf '%s=%s\n' "$k" "${S[$k]}" >> "$SECRETS_FILE"
        fi
    done
    if [ "${#MINTED[@]}" -gt 0 ]; then
        echo "  → generated ${#MINTED[@]} secret(s) and saved them to $SECRETS_FILE (keep it):"
        printf '      %s\n' "${MINTED[@]}"
    fi
    if [ -z "$REVERB_APP_KEY_ARG" ] && [[ " ${MINTED[*]:-} " == *" REVERB_APP_KEY "* ]]; then
        echo "  ! REVERB_APP_KEY was generated. The browser's key is baked into the laravel"
        echo "    image at build time; live chat connects only if the image was built with"
        echo "    VITE_REVERB_APP_KEY set to this value. If it was built with another, re-run"
        echo "    with --reverb-app-key <that value> (README.md, \"Live chat\")."
    fi
else
    echo "  → release '$RELEASE' exists: keeping its secrets$([ -n "$SECRETS_FILE" ] && echo ", overridden by $SECRETS_FILE")"
fi

# Secrets go to helm in a mode-600 values file, not as --set arguments (which
# anyone on the host can read from the process list, and which treat commas in
# a value as separators).
VALUES_TMP="$(mktemp)"
trap 'rm -f "$VALUES_TMP"' EXIT
chmod 600 "$VALUES_TMP"
if [ "${#S[@]}" -gt 0 ]; then
    sq="'"
    echo "secrets:" > "$VALUES_TMP"
    for k in "${!S[@]}"; do
        v="${S[$k]}"
        # YAML single-quoted scalar: a quote inside is written twice.
        printf "  %s: '%s'\n" "${KEYMAP[$k]}" "${v//$sq/$sq$sq}" >> "$VALUES_TMP"
    done
fi

# ─── 4. Install Helm chart ───────────────────────────────────────────
echo
echo "→ installing chart"
shopt -s nullglob
charts=(chart/*.tgz)
[ "${#charts[@]}" -ge 1 ] || die "no chart .tgz found in chart/"
CHART="${charts[0]}"
echo "  chart: $CHART"

# global.createNamespace=false: --create-namespace makes the namespace outside
# the release, and Helm will not adopt a namespace it did not create, so a chart
# that also renders it fails with "exists and cannot be imported".
# global.namespace: the chart puts its objects in this, not in the release
# namespace, so the two have to be told the same thing.
HELM_ARGS=(
    upgrade --install "$RELEASE" "$CHART"
    -f values-airgap.yaml
    --create-namespace --namespace "$NAMESPACE"
    --set "global.createNamespace=false"
    --set "global.namespace=$NAMESPACE"
    --set "global.imageRegistry=$REGISTRY"
    --timeout "${HELM_TIMEOUT:-20m}"
)
if [ "$EXISTING" -eq 1 ]; then
    HELM_ARGS+=(--reuse-values)
fi
if [ -s "$VALUES_TMP" ]; then
    HELM_ARGS+=(-f "$VALUES_TMP")
fi

helm "${HELM_ARGS[@]}"

# ─── 5. Status ───────────────────────────────────────────────────────
echo
echo "→ waiting for pods to roll out (max 5 min)"
kubectl -n "$NAMESPACE" rollout status statefulset/georag-postgresql --timeout=300s || true
kubectl -n "$NAMESPACE" rollout status deployment/georag-fastapi --timeout=300s || true

echo
echo "→ status"
kubectl -n "$NAMESPACE" get pods || true

echo
echo "============================================================"
echo "  Install complete. Ingress: kubectl -n $NAMESPACE get ingress"
echo
echo "  Still to do (README.md):"
echo "    - the embedding and reranker pods need their model weights (\"Model weights\")"
echo "    - the Hatchet token (\"Hatchet token\"): fastapi and the worker cannot"
echo "      authenticate to the engine until it is set"
echo "============================================================"
