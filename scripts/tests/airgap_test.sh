#!/usr/bin/env bash
# Discrimination tests for the air-gap path: scripts/build_airgap_bundle.sh,
# scripts/verify_airgap_bundle.sh and airgap/install.sh, run END TO END with
# docker, helm, k3s, kubectl and sudo replaced by recording stubs.
#
# Nothing here can pull an image or reach a cluster, so what is asserted is the
# part that was wrong and that no stub can mask: the NAMES. Each case below
# reproduces a defect the scripts shipped with and asserts the fix:
#
#   * install.sh rebuilt image references from tarball names with
#     `tr '_' '/' | sed 's/\.\([^.]*\)$/:\1/'`, which turns
#     qdrant_qdrant_v1.19.1.tar into qdrant/qdrant/v1.19:1, so every `tag`
#     failed and the install aborted. The k3s stub below, like containerd,
#     names an imported image by its docker.io-normalised reference and fails
#     `ctr image tag` on any other source name.
#   * install.sh never supplied secrets.martinDbPassword (the chart's
#     `required`), and ignored HATCHET_CLIENT_TOKEN / QDRANT_API_KEY /
#     COHERE_API_KEY in a secrets file.
#   * install.sh ran `helm install`, so the README's "upgrades an existing
#     release" was false, and a missing --secrets-file silently minted random
#     secrets.
#   * verify_airgap_bundle.sh demanded >= 10 images; the chart renders 9.
#   * build_airgap_bundle.sh discarded helm's stderr, so a failed render
#     produced an empty bundle.
#
# Run: bash scripts/tests/airgap_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PASS=0
FAIL=0
ok()    { printf '\033[32m  ✓ %s\033[0m\n' "$*"; PASS=$((PASS+1)); }
bad()   { printf '\033[31m  ✗ %s\033[0m\n' "$*"; FAIL=$((FAIL+1)); }
case_() { printf '\033[34m%s\033[0m\n' "$*"; }
# expect <description> <command...> — passes when the command succeeds.
expect() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }
# refute <description> <command...> — passes when the command fails.
refute() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$d"; else ok "$d"; fi; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ─── stubs ───────────────────────────────────────────────────────────
STUBS_K3S="$WORK/stubs-k3s"
STUBS_DOCKER="$WORK/stubs-docker"   # same, without k3s: selects the Docker runtime
mkdir -p "$STUBS_K3S" "$STUBS_DOCKER"

cat > "$STUBS_K3S/docker" <<'STUB'
#!/usr/bin/env bash
S="${FAKE_STATE:?}"; mkdir -p "$S"
echo "docker $*" >> "$S/calls"
cmd="${1:-}"; shift || true
case "$cmd" in
  run)
    args=("$@"); sub=""
    for a in "${args[@]}"; do case "$a" in template|package) sub="$a"; break ;; esac; done
    case "$sub" in
      template)
        if [ "${FAKE_HELM_FAIL:-0}" = 1 ]; then
          echo "Error: execution error at (georag/templates/secrets.yaml:21:42): secrets.reverbAppKey is required" >&2
          exit 1
        fi
        if printf '%s\n' "${args[@]}" | grep -q 'values-airgap.yaml'; then
          cat "$FAKE_AIRGAP_RENDER"
        else
          cat "$FAKE_DEFAULT_RENDER"
        fi ;;
      package)
        out=""; for a in "${args[@]}"; do case "$a" in *:/out) out="${a%:/out}" ;; esac; done
        d="$(mktemp -d)"; mkdir -p "$d/georag"; echo "name: georag" > "$d/georag/Chart.yaml"
        tar -C "$d" -czf "$out/georag-0.2.0.tgz" georag; rm -rf "$d" ;;
      *) echo "stub docker run: unhandled: $*" >&2; exit 1 ;;
    esac ;;
  pull)
    [ "${FAKE_PULL_FAIL:-}" = "$1" ] && { echo "pull access denied for $1" >&2; exit 1; }
    echo "$1" >> "$S/local-images" ;;
  tag)
    grep -Fxq "$1" "$S/local-images" 2>/dev/null || { echo "Error response from daemon: No such image: $1" >&2; exit 1; }
    echo "$2" >> "$S/local-images"; echo "TAG $1 $2" >> "$S/tags" ;;
  save)  # docker save -o FILE REF   (a real tar, holding one file: the reference it carries)
    grep -Fxq "$3" "$S/local-images" 2>/dev/null || { echo "Error response from daemon: No such image: $3" >&2; exit 1; }
    d="$(mktemp -d)"; echo "$3" > "$d/ref.txt"; tar -C "$d" -cf "$2" ref.txt; rm -rf "$d" ;;
  load)  # docker load -i FILE
    tar -xOf "$2" ref.txt >> "$S/local-images" ;;
  *) echo "stub docker: unhandled: $cmd $*" >&2; exit 1 ;;
esac
STUB

cat > "$STUBS_K3S/k3s" <<'STUB'
#!/usr/bin/env bash
S="${FAKE_STATE:?}"; mkdir -p "$S"
echo "k3s $*" >> "$S/calls"
# containerd names an imported docker-format image by its docker.io-normalised
# reference. Written here independently of install.sh's canonical_ref on purpose.
canon() {
  local r="$1" first="${1%%/*}"
  case "$r" in
    */*) case "$first" in *.*|*:*|localhost) echo "$r" ;; *) echo "docker.io/$r" ;; esac ;;
    *)   echo "docker.io/library/$r" ;;
  esac
}
[ "$1" = ctr ] && [ "$2" = image ] || { echo "stub k3s: unhandled: $*" >&2; exit 1; }
shift 2
case "$1" in
  import) canon "$(tar -xOf "$2" ref.txt)" >> "$S/ctr-images" ;;
  ls)     cat "$S/ctr-images" 2>/dev/null ;;
  tag)
    shift; [ "$1" = --force ] && shift
    grep -Fxq "$1" "$S/ctr-images" 2>/dev/null || { echo "ctr: image \"$1\": not found" >&2; exit 1; }
    echo "$2" >> "$S/ctr-images"; echo "TAG $1 $2" >> "$S/tags" ;;
  *) echo "stub k3s: unhandled ctr image $*" >&2; exit 1 ;;
esac
STUB

cat > "$STUBS_K3S/helm" <<'STUB'
#!/usr/bin/env bash
S="${FAKE_STATE:?}"; mkdir -p "$S"
echo "helm $*" >> "$S/calls"
case "$1" in
  status) [ "${FAKE_HELM_EXISTING:-0}" = 1 ] ;;
  upgrade)
    printf '%s\n' "$@" > "$S/helm-args"
    prev=""; last=""
    for a in "$@"; do [ "$prev" = -f ] && last="$a"; prev="$a"; done
    if [ -n "$last" ] && [ "$last" != values-airgap.yaml ]; then
      cp "$last" "$S/secrets-values.yaml"; stat -c %a "$last" > "$S/secrets-values.mode"
    fi ;;
esac
STUB

cat > "$STUBS_K3S/kubectl" <<'STUB'
#!/usr/bin/env bash
echo "kubectl $*" >> "${FAKE_STATE:?}/calls"
STUB

cat > "$STUBS_K3S/sudo" <<'STUB'
#!/usr/bin/env bash
exec "$@"
STUB

chmod +x "$STUBS_K3S"/*
cp "$STUBS_K3S/docker" "$STUBS_K3S/helm" "$STUBS_K3S/kubectl" "$STUBS_K3S/sudo" "$STUBS_DOCKER/"

# ─── fixture: a repo with just what the bundler reads, and two renders ─
cat > "$WORK/render-airgap.txt" <<'EOF'
        image: registry.internal.local/georag/redis:8.6-alpine
          image: "registry.internal.local/georag/qdrant/qdrant:v1.19.1"
          image: registry.internal.local/georag/ghcr.io/maplibre/martin:1.11.0
          image: registry.internal.local/georag/ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.91.2
          image: registry.internal.local/georag/chrislusf/seaweedfs:4.35
EOF
PIN="sha256:00705eb1e9ea653aaa9473703fd24df7350eacca1a1a0b5e9a7dece0f8d27c70"
cat > "$WORK/render-default.txt" <<EOF
        image: redis:8.6-alpine
          image: qdrant/qdrant:v1.19.1
          image: ghcr.io/maplibre/martin:1.11.0
          image: ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.91.2@${PIN}
          image: chrislusf/seaweedfs:4.35
EOF

make_repo() {
  local d="$1"
  mkdir -p "$d/scripts" "$d/airgap" "$d/charts/georag"
  cp "$REPO_ROOT/scripts/build_airgap_bundle.sh" "$REPO_ROOT/scripts/verify_airgap_bundle.sh" "$d/scripts/"
  cp "$REPO_ROOT/airgap/install.sh" "$REPO_ROOT/airgap/README.md" "$d/airgap/"
  cp "$REPO_ROOT/charts/georag/values-airgap.yaml" "$d/charts/georag/"
}

# run_build <repo> <state> [env assignments...] -> output in $WORK/build.out, status returned
run_build() {
  local repo="$1" state="$2"; shift 2
  env PATH="$STUBS_K3S:$PATH" FAKE_STATE="$state" \
      FAKE_AIRGAP_RENDER="$WORK/render-airgap.txt" FAKE_DEFAULT_RENDER="$WORK/render-default.txt" \
      "$@" bash "$repo/scripts/build_airgap_bundle.sh" --version vtest --out "$repo/dist" \
      > "$WORK/build.out" 2>&1
}

REPO="$WORK/repo"; make_repo "$REPO"
BSTATE="$WORK/state-build"

# ---------------------------------------------------------------------------
case_ "bundler: builds a bundle whose index names every image"
run_build "$REPO" "$BSTATE"; RC=$?
if [ "$RC" -eq 0 ]; then ok "build succeeds"; else bad "build failed (rc=$RC): $(tail -5 "$WORK/build.out")"; fi
TARBALL="$REPO/dist/georag-airgap-vtest.tar.gz"
[ -f "$TARBALL" ] && ok "tarball written" || bad "no tarball at $TARBALL"
EX="$WORK/extract"; mkdir -p "$EX"; tar -C "$EX" -xzf "$TARBALL" 2>/dev/null
BUNDLE="$EX/_stage_vtest"
IDX="$BUNDLE/images/index.txt"
expect "index maps the qdrant tarball to its real reference" \
  grep -qxF "$(printf 'qdrant_qdrant_v1.19.1.tar\tqdrant/qdrant:v1.19.1')" "$IDX"
expect "index maps a bare library image" \
  grep -qxF "$(printf 'redis_8.6-alpine.tar\tredis:8.6-alpine')" "$IDX"
expect "index maps a registry-qualified image" \
  grep -qxF "$(printf 'ghcr.io_maplibre_martin_1.11.0.tar\tghcr.io/maplibre/martin:1.11.0')" "$IDX"
expect "5 images, 5 tarballs" test "$(ls "$BUNDLE"/images/*.tar | wc -l)" -eq 5
# make_repo copies install.sh from a checkout, where it is mode 0644; the README
# tells the operator to run ./install.sh.
expect "install.sh is executable in the bundle" test -x "$BUNDLE/install.sh"

# ---------------------------------------------------------------------------
case_ "bundler: a digest-pinned image is pulled by digest and shipped by tag"
# values-airgap names it by tag (a loaded image does not keep its registry
# manifest digest); the pin is verified when the bundle is built.
expect "pulled by the pinned digest" \
  grep -qxF "docker pull ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.91.2@${PIN}" "$BSTATE/calls"
expect "re-tagged to the tag-only name before saving" \
  grep -qxF "TAG ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.91.2@${PIN} ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.91.2" "$BSTATE/tags"
expect "the index carries the tag-only reference" \
  grep -qF "$(printf '\tghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.91.2')" "$IDX"
expect "MANIFEST records the pin" grep -qF "$PIN" "$BUNDLE/MANIFEST.yaml"
expect "MANIFEST image_count is the number of tarballs" grep -qx 'image_count=5' "$BUNDLE/MANIFEST.yaml"

# ---------------------------------------------------------------------------
case_ "bundler: fails loudly instead of building an empty bundle"
R2="$WORK/repo2"; make_repo "$R2"
run_build "$R2" "$WORK/state-b2" FAKE_HELM_FAIL=1; RC=$?
if [ "$RC" -ne 0 ] && grep -q "helm template failed" "$WORK/build.out" && grep -q "reverbAppKey is required" "$WORK/build.out"; then
  ok "a failed render stops the build and shows helm's own message"
else
  bad "a failed render must stop the build; rc=$RC: $(tail -5 "$WORK/build.out")"
fi
refute "no tarball was produced" test -e "$R2/dist/georag-airgap-vtest.tar.gz"

# values.yaml bumped, values-airgap.yaml not: the bundle would ship the old engine.
R3="$WORK/repo3"; make_repo "$R3"
sed 's#hatchet-lite:v0.91.2@#hatchet-lite:v0.92.0@#' "$WORK/render-default.txt" > "$WORK/render-default-drift.txt"
env PATH="$STUBS_K3S:$PATH" FAKE_STATE="$WORK/state-b3" \
    FAKE_AIRGAP_RENDER="$WORK/render-airgap.txt" FAKE_DEFAULT_RENDER="$WORK/render-default-drift.txt" \
    bash "$R3/scripts/build_airgap_bundle.sh" --version vtest --out "$R3/dist" > "$WORK/build.out" 2>&1; RC=$?
if [ "$RC" -ne 0 ] && grep -q "values.yaml pins" "$WORK/build.out" && grep -q "values-airgap.yaml" "$WORK/build.out"; then
  ok "a digest pin with no matching air-gap tag fails the build"
else
  bad "pin/tag drift must fail the build; rc=$RC: $(tail -5 "$WORK/build.out")"
fi

# ---------------------------------------------------------------------------
case_ "verifier: accepts a correct 5-image bundle (it demanded >= 10)"
OUT="$(bash "$REPO/scripts/verify_airgap_bundle.sh" "$TARBALL" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ]; then ok "verify passes"; else bad "verify failed (rc=$RC): $(echo "$OUT" | grep FAIL)"; fi
expect "the old fixed threshold is gone" bash -c "! grep -q 'ge 10' '$REPO_ROOT/scripts/verify_airgap_bundle.sh'"

# a tarball missing from the bundle, and a bundle without its index
V2="$WORK/v2"; mkdir -p "$V2"; tar -C "$V2" -xzf "$TARBALL"
rm "$V2/_stage_vtest/images/redis_8.6-alpine.tar"
tar -C "$V2" -czf "$WORK/missing-tar.tar.gz" _stage_vtest
OUT="$(bash "$REPO/scripts/verify_airgap_bundle.sh" "$WORK/missing-tar.tar.gz" 2>&1)"; RC=$?
if [ "$RC" -ne 0 ] && grep -q "index lists missing redis_8.6-alpine.tar" <<<"$OUT"; then ok "rejects an index entry with no tarball"; else bad "should reject; rc=$RC: $OUT"; fi

V3="$WORK/v3"; mkdir -p "$V3"; tar -C "$V3" -xzf "$TARBALL"
rm "$V3/_stage_vtest/images/index.txt"
tar -C "$V3" -czf "$WORK/no-index.tar.gz" _stage_vtest
OUT="$(bash "$REPO/scripts/verify_airgap_bundle.sh" "$WORK/no-index.tar.gz" 2>&1)"; RC=$?
if [ "$RC" -ne 0 ] && grep -q "index.txt" <<<"$OUT"; then ok "rejects a bundle without images/index.txt"; else bad "should reject; rc=$RC"; fi

# ─── install.sh ──────────────────────────────────────────────────────
# fresh_bundle <dir> — a private copy of the extracted bundle to run in.
fresh_bundle() { rm -rf "$1"; cp -r "$BUNDLE" "$1"; }
# run_install <bundledir> <state> <stubs> [ENV=VAL ...] -- <install.sh args...>
run_install() {
  local dir="$1" state="$2" stubs="$3"; shift 3
  local envs=()
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
  [ "${1:-}" = "--" ] && shift
  ( cd "$dir" && env PATH="$stubs:$PATH" FAKE_STATE="$state" "${envs[@]}" bash ./install.sh "$@" ) > "$WORK/install.out" 2>&1
}
REG="registry.internal.local/georag"

case_ "install (K3s): names every image from the index, not from the file name"
I1="$WORK/i1"; fresh_bundle "$I1"; S1="$WORK/state-i1"
run_install "$I1" "$S1" "$STUBS_K3S" --; RC=$?
if [ "$RC" -eq 0 ]; then ok "install succeeds"; else bad "install failed (rc=$RC): $(tail -6 "$WORK/install.out")"; fi
# the bug: qdrant_qdrant_v1.19.1.tar -> qdrant/qdrant/v1.19:1, and `ctr image tag` fails
expect "qdrant: docker.io/qdrant/qdrant:v1.19.1 -> ${REG}/qdrant/qdrant:v1.19.1" \
  grep -qxF "TAG docker.io/qdrant/qdrant:v1.19.1 ${REG}/qdrant/qdrant:v1.19.1" "$S1/tags"
expect "library image gets the docker.io/library/ form" \
  grep -qxF "TAG docker.io/library/redis:8.6-alpine ${REG}/redis:8.6-alpine" "$S1/tags"
expect "a ghcr.io image is taken as it is" \
  grep -qxF "TAG ghcr.io/maplibre/martin:1.11.0 ${REG}/ghcr.io/maplibre/martin:1.11.0" "$S1/tags"
expect "the digest-pinned engine is tagged by its tag-only name" \
  grep -qxF "TAG ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.91.2 ${REG}/ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.91.2" "$S1/tags"
expect "all five images were tagged" test "$(wc -l < "$S1/tags")" -eq 5

case_ "install: helm upgrade --install, with the chart's required secrets"
ARGS="$S1/helm-args"
expect "helm upgrade --install (the README says it upgrades)" bash -c "head -2 '$ARGS' | tr '\n' ' ' | grep -q 'upgrade --install'"
expect "the bundled chart is installed" grep -qx 'chart/georag-0.2.0.tgz' "$ARGS"
expect "air-gap values" grep -qx 'values-airgap.yaml' "$ARGS"
expect "--create-namespace" grep -qx -- '--create-namespace' "$ARGS"
expect "the chart does not also create the namespace Helm just made" grep -qx 'global.createNamespace=false' "$ARGS"
expect "global.namespace follows --namespace" grep -qx 'global.namespace=georag' "$ARGS"
expect "registry prefix is passed" grep -qx "global.imageRegistry=${REG}" "$ARGS"
expect "a timeout that outlasts the schema hook" grep -qx '20m' "$ARGS"
refute "a first install does not reuse values" grep -qx -- '--reuse-values' "$ARGS"
refute "secrets are not on the command line" grep -q 'secrets\.' "$ARGS"
V="$S1/secrets-values.yaml"
expect "martinDbPassword is minted (the chart requires it)" grep -qE "^  martinDbPassword: '[0-9a-f]{64}'$" "$V"
for key in postgresPassword pgAppPassword redisPassword fastapiServiceKey laravelAppKey hatchetDbPassword reverbAppSecret reverbAppKey; do
  expect "$key is minted" grep -qE "^  ${key}: '.+'$" "$V"
done
expect "values file is mode 600" test "$(cat "$S1/secrets-values.mode")" = 600
SF="$I1/georag-secrets.env"
expect "secrets are saved for the next run" test -s "$SF"
expect "...with mode 600" test "$(stat -c %a "$SF")" = 600
expect "...including MARTIN_DB_PASSWORD" grep -qE '^MARTIN_DB_PASSWORD=[0-9a-f]{64}$' "$SF"
expect "a generated REVERB_APP_KEY is called out" grep -q 'REVERB_APP_KEY was generated' "$WORK/install.out"

case_ "install: a re-run reuses the saved secrets"
cp "$SF" "$WORK/secrets-before.env"
S1b="$WORK/state-i1b"
run_install "$I1" "$S1b" "$STUBS_K3S" --; RC=$?
if [ "$RC" -eq 0 ]; then ok "second run succeeds"; else bad "second run failed (rc=$RC)"; fi
expect "the secrets file is unchanged" cmp -s "$SF" "$WORK/secrets-before.env"
expect "the same martinDbPassword goes to helm" \
  grep -qF "martinDbPassword: '$(grep '^MARTIN_DB_PASSWORD=' "$SF" | cut -d= -f2-)'" "$S1b/secrets-values.yaml"

case_ "install: --secrets-file maps every key, and an existing release is upgraded in place"
I2="$WORK/i2"; fresh_bundle "$I2"; S2="$WORK/state-i2"
cat > "$WORK/my-secrets.env" <<'EOF'
# comment
POSTGRES_PASSWORD=pg==/+pw
PG_APP_PASSWORD=app
MARTIN_DB_PASSWORD=abc123
REDIS_PASSWORD=redis
FASTAPI_SERVICE_KEY=svc-key-0123456789012345678901234567
LARAVEL_APP_KEY=base64:AAAA==
HATCHET_CLIENT_TOKEN=eyJhbGciOi.tok.en
HATCHET_DB_PASSWORD=hdb
QDRANT_API_KEY="q-key"
COHERE_API_KEY=co'he,re
REVERB_APP_ID=my-app
REVERB_APP_KEY=browserkey
REVERB_APP_SECRET=rsecret
NOT_A_KEY=ignored
EOF
run_install "$I2" "$S2" "$STUBS_K3S" FAKE_HELM_EXISTING=1 -- --secrets-file "$WORK/my-secrets.env"; RC=$?
if [ "$RC" -eq 0 ]; then ok "install with a secrets file succeeds"; else bad "failed (rc=$RC): $(tail -6 "$WORK/install.out")"; fi
V="$S2/secrets-values.yaml"
expect "--reuse-values on an existing release" grep -qx -- '--reuse-values' "$S2/helm-args"
expect "HATCHET_CLIENT_TOKEN reaches the chart" grep -qx "  hatchetClientToken: 'eyJhbGciOi.tok.en'" "$V"
expect "QDRANT_API_KEY reaches the chart (quotes stripped)" grep -qx "  qdrantApiKey: 'q-key'" "$V"
expect "COHERE_API_KEY reaches the chart, quote and comma intact" grep -qx "  cohereApiKey: 'co''he,re'" "$V"
expect "base64 padding survives" grep -qx "  laravelAppKey: 'base64:AAAA=='" "$V"
expect "'=' and '/' in a password survive" grep -qx "  postgresPassword: 'pg==/+pw'" "$V"
expect "REVERB_* reach the chart" bash -c "grep -qx \"  reverbAppKey: 'browserkey'\" '$V' && grep -qx \"  reverbAppSecret: 'rsecret'\" '$V' && grep -qx \"  reverbAppId: 'my-app'\" '$V'"
expect "an unknown key is reported, not silently dropped" grep -q 'unknown key NOT_A_KEY' "$WORK/install.out"
refute "nothing is generated for an existing release" test -e "$I2/georag-secrets.env"

case_ "install: an existing release with no secrets file keeps its own secrets"
I3="$WORK/i3"; fresh_bundle "$I3"; S3="$WORK/state-i3"
run_install "$I3" "$S3" "$STUBS_K3S" FAKE_HELM_EXISTING=1 --; RC=$?
if [ "$RC" -eq 0 ]; then ok "upgrade succeeds"; else bad "upgrade failed (rc=$RC): $(tail -6 "$WORK/install.out")"; fi
expect "--reuse-values" grep -qx -- '--reuse-values' "$S3/helm-args"
refute "no secrets file is written, so no password is rotated" test -e "$I3/georag-secrets.env"
refute "no secrets values are passed" test -e "$S3/secrets-values.yaml"

case_ "install: --reverb-app-key is used as given"
I4="$WORK/i4"; fresh_bundle "$I4"; S4="$WORK/state-i4"
run_install "$I4" "$S4" "$STUBS_K3S" -- --reverb-app-key mybuiltkey --namespace chat; RC=$?
if [ "$RC" -eq 0 ]; then ok "install succeeds"; else bad "failed (rc=$RC): $(tail -6 "$WORK/install.out")"; fi
expect "the key the image was built with is passed" grep -qx "  reverbAppKey: 'mybuiltkey'" "$S4/secrets-values.yaml"
refute "...and the 'generated' warning is not shown" grep -q 'REVERB_APP_KEY was generated' "$WORK/install.out"
expect "--namespace reaches both the release and the chart" bash -c "grep -qx chat '$S4/helm-args' && grep -qx 'global.namespace=chat' '$S4/helm-args'"

case_ "install: refuses instead of guessing"
I5="$WORK/i5"; fresh_bundle "$I5"; S5="$WORK/state-i5"
run_install "$I5" "$S5" "$STUBS_K3S" -- --secrets-file "$WORK/does-not-exist.env"; RC=$?
if [ "$RC" -ne 0 ] && grep -q 'no such file' "$WORK/install.out"; then ok "a missing --secrets-file is an error (it used to mint random secrets silently)"; else bad "should fail; rc=$RC"; fi
refute "helm was not run" grep -q '^helm upgrade' "$S5/calls"

I6="$WORK/i6"; fresh_bundle "$I6"; S6="$WORK/state-i6"; rm "$I6/images/index.txt"
run_install "$I6" "$S6" "$STUBS_K3S" --; RC=$?
if [ "$RC" -ne 0 ] && grep -q 'index.txt missing' "$WORK/install.out"; then ok "a bundle without index.txt is rejected, naming the cause"; else bad "should fail; rc=$RC"; fi

I7="$WORK/i7"; fresh_bundle "$I7"; S7="$WORK/state-i7"; rm "$I7/images/redis_8.6-alpine.tar"
run_install "$I7" "$S7" "$STUBS_K3S" --; RC=$?
if [ "$RC" -ne 0 ] && grep -q 'redis_8.6-alpine.tar is listed' "$WORK/install.out"; then ok "an indexed tarball that is missing is rejected"; else bad "should fail; rc=$RC"; fi

case_ "install (Docker): load then tag by the reference as docker knows it"
I8="$WORK/i8"; fresh_bundle "$I8"; S8="$WORK/state-i8"
run_install "$I8" "$S8" "$STUBS_DOCKER" -- ; RC=$?
if [ "$RC" -eq 0 ]; then ok "install succeeds on the Docker runtime"; else bad "failed (rc=$RC): $(tail -6 "$WORK/install.out")"; fi
expect "docker tag qdrant/qdrant:v1.19.1 ${REG}/qdrant/qdrant:v1.19.1" \
  grep -qxF "TAG qdrant/qdrant:v1.19.1 ${REG}/qdrant/qdrant:v1.19.1" "$S8/tags"
expect "no k3s call was made" bash -c "! grep -q '^k3s' '$S8/calls'"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
