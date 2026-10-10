#!/usr/bin/env bash
# Discrimination tests for scripts/check-internal-urls.py.
#
# The checker used to read Terraform only. The Helm chart then turned out to
# have the same bug in every form the checker exists for (FASTAPI_INTERNAL_URL
# missing on Horizon, MARTIN_INTERNAL_URL on Octane, LARAVEL_INTERNAL_URL on
# FastAPI and the Hatchet worker), and nothing noticed because nothing looked.
# A checker that prints OK on the tree it was written against has shown
# nothing, so each case reproduces one of those shapes against a scratch copy
# of the committed renders and asserts it is rejected -- and that the
# Terraform half still is.
#
# Run: bash scripts/tests/internal_urls_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECKER="${REPO_ROOT}/scripts/check-internal-urls.py"
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"

PASS=0
FAIL=0
ok()    { printf '\033[32m  ✓ %s\033[0m\n' "$*"; PASS=$((PASS+1)); }
bad()   { printf '\033[31m  ✗ %s\033[0m\n' "$*"; FAIL=$((FAIL+1)); }
case_() { printf '\033[34m%s\033[0m\n' "$*"; }

# A scratch root: real config/ and src/ (read only, symlinked), and private
# copies of the two things the cases mutate.
make_fixture() {
  local d
  d="$(mktemp -d)"
  mkdir -p "$d/scripts" "$d/deploy/aws" "$d/kubernetes"
  cp "$CHECKER" "$d/scripts/check-internal-urls.py"
  cp -r "$REPO_ROOT/deploy/aws/terraform" "$d/deploy/aws/terraform"
  cp -r "$REPO_ROOT/kubernetes/manifests" "$d/kubernetes/manifests"
  ln -s "$REPO_ROOT/config" "$d/config"
  ln -s "$REPO_ROOT/src" "$d/src"
  echo "$d"
}

run_checker() { "$PYTHON" "$1/scripts/check-internal-urls.py" "$1" 2>&1; }

# unset_env <fixture> <component> <VAR> [flavor...]
# Delete one literal env entry from one workload in the given renders (all three
# by default). Fails loudly if it matched nothing: a mutation that does nothing
# turns a test into a no-op that passes for the wrong reason.
unset_env() {
  local d="$1" comp="$2" var="$3"; shift 3
  local flavors=("$@"); [ "${#flavors[@]}" -eq 0 ] && flavors=(k3s vanilla airgap)
  "$PYTHON" -I - "$d" "$comp" "$var" "${flavors[@]}" <<'PY'
import re, sys
root, comp, var, *flavors = sys.argv[1:]
changed = 0
for flavor in flavors:
    path = f"{root}/kubernetes/manifests/{flavor}.yaml"
    docs = open(path, encoding="utf-8").read().split("\n---\n")
    for i, doc in enumerate(docs):
        if not re.search(r"^kind: (Deployment|StatefulSet)$", doc, re.M):
            continue
        if not re.search(rf"app\.kubernetes\.io/component: {re.escape(comp)}$", doc, re.M):
            continue
        new = re.sub(rf"^( *)- name: {var}\n\1  value: .*\n", "", doc, flags=re.M)
        if new != doc:
            docs[i] = new
            changed += 1
    open(path, "w", encoding="utf-8").write("\n---\n".join(docs))
if not changed:
    print(f"mutation matched nothing: {comp} {var} in {flavors}", file=sys.stderr)
    sys.exit(2)
PY
}

# set_env <fixture> <component> <VAR> <value>   (all flavors)
set_env() {
  local d="$1" comp="$2" var="$3" value="$4"
  "$PYTHON" -I - "$d" "$comp" "$var" "$value" <<'PY'
import re, sys
root, comp, var, value = sys.argv[1:]
changed = 0
for flavor in ("k3s", "vanilla", "airgap"):
    path = f"{root}/kubernetes/manifests/{flavor}.yaml"
    docs = open(path, encoding="utf-8").read().split("\n---\n")
    for i, doc in enumerate(docs):
        if not re.search(r"^kind: (Deployment|StatefulSet)$", doc, re.M):
            continue
        if not re.search(rf"app\.kubernetes\.io/component: {re.escape(comp)}$", doc, re.M):
            continue
        new = re.sub(rf'^( *- name: {var}\n *value: ).*$', lambda m: m.group(1) + '"' + value + '"', doc, flags=re.M)
        if new != doc:
            docs[i] = new
            changed += 1
    open(path, "w", encoding="utf-8").write("\n---\n".join(docs))
if not changed:
    print(f"mutation matched nothing: {comp} {var}", file=sys.stderr)
    sys.exit(2)
PY
}

expect_rejected() {  # <name> <fixture> <needle>...
  local name="$1" d="$2"; shift 2
  local out rc n ok=1
  out="$(run_checker "$d")"; rc=$?
  [ "$rc" -eq 1 ] || ok=0
  for n in "$@"; do grep -qF -- "$n" <<<"$out" || ok=0; done
  if [ "$ok" -eq 1 ]; then ok "$name"; else bad "$name (rc=$rc)"; printf '%s\n' "$out" | sed 's/^/        /'; fi
  rm -rf "$d"
}

# ---------------------------------------------------------------------------
case_ "the tree as it stands"
d="$(make_fixture)"
out="$(run_checker "$d")"; rc=$?
if [ "$rc" -eq 0 ] && grep -q "in Terraform and in the Helm chart" <<<"$out"; then
  ok "passes, and says it read the chart as well as Terraform"
else
  bad "unmodified tree should pass (rc=$rc)"; printf '%s\n' "$out" | sed 's/^/        /'
fi
rm -rf "$d"

# ---------------------------------------------------------------------------
case_ "chart: the queued chat job's FASTAPI_INTERNAL_URL (Horizon)"
# Octane only dispatches StreamQueryFromFastApi; Horizon runs it. With the
# variable missing there, every question is accepted, queued, and dies on
# `Name or service not known`.
d="$(make_fixture)"; unset_env "$d" laravel-horizon FASTAPI_INTERNAL_URL || exit 2
expect_rejected "Horizon without FASTAPI_INTERNAL_URL is rejected, in every flavor" "$d" \
  "[Helm chart: k3s, vanilla, airgap]" "FASTAPI_INTERNAL_URL is set on laravel-octane" "NOT on laravel-horizon"

d="$(make_fixture)"; unset_env "$d" laravel-horizon FASTAPI_INTERNAL_URL vanilla || exit 2
expect_rejected "...and a single drifted flavor is named alone" "$d" \
  "[Helm chart: vanilla]" "NOT on laravel-horizon"

# ---------------------------------------------------------------------------
case_ "chart: MARTIN_INTERNAL_URL (Octane, the tile proxy)"
d="$(make_fixture)"; unset_env "$d" laravel-octane MARTIN_INTERNAL_URL || exit 2
expect_rejected "no workload sets MARTIN_INTERNAL_URL" "$d" \
  "MARTIN_INTERNAL_URL" "is not set in the Helm chart"

# ---------------------------------------------------------------------------
case_ "chart: LARAVEL_INTERNAL_URL (FastAPI and the Hatchet worker)"
# Every ingestion-progress and workspace-data-updated callback goes through it,
# and its fallback is http://laravel.test.
d="$(make_fixture)"; unset_env "$d" hatchet-worker LARAVEL_INTERNAL_URL || exit 2
expect_rejected "set on FastAPI but forgotten on the worker is rejected" "$d" \
  "LARAVEL_INTERNAL_URL is set on fastapi" "NOT on hatchet-worker"

d="$(make_fixture)"; unset_env "$d" hatchet-worker LARAVEL_INTERNAL_URL || exit 2; unset_env "$d" fastapi LARAVEL_INTERNAL_URL || exit 2
expect_rejected "set nowhere is rejected" "$d" \
  "_DEFAULT_LARAVEL_URL" "none of which the Helm chart sets"

# ---------------------------------------------------------------------------
case_ "chart: a value copied from docker-compose.yml"
# Present is not the same as right: compose's `http://fastapi:8000` resolves on
# the compose network and nowhere else. A Service in this chart is
# <release>-fastapi.
d="$(make_fixture)"; set_env "$d" laravel-horizon FASTAPI_INTERNAL_URL "http://fastapi:8000" || exit 2
expect_rejected "an internal URL naming a bare compose host is rejected" "$d" \
  "FASTAPI_INTERNAL_URL = 'http://fastapi:8000' on laravel-horizon names a compose host"

# ---------------------------------------------------------------------------
case_ "deliberate asymmetries stay allowed"
# laravel-reverb has no FASTAPI_INTERNAL_URL / MARTIN_INTERNAL_URL and Horizon no
# MARTIN_INTERNAL_URL (PER_SERVICE_ALLOWED); the first case already proves the
# unmodified chart passes with exactly those gaps. The sidecars run the fastapi
# image and set none of these on purpose:
d="$(make_fixture)"
if grep -q 'app.kubernetes.io/component: sparse' "$d/kubernetes/manifests/vanilla.yaml"; then
  out="$(run_checker "$d")"; rc=$?
  if [ "$rc" -eq 0 ]; then ok "the model sidecars are not callers and are not asked to be"; else bad "sidecars should not trip the rule"; printf '%s\n' "$out" | sed 's/^/        /'; fi
else
  bad "fixture has no sparse sidecar; this case proves nothing"
fi
rm -rf "$d"

# ---------------------------------------------------------------------------
case_ "Terraform is still checked"
# Dropping FASTAPI_INTERNAL_URL from laravel-horizon's block in config.tf is the
# 2026-09-16 incident this checker was written for.
d="$(make_fixture)"
"$PYTHON" -I - "$d/deploy/aws/terraform/config.tf" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
start = text.index("laravel-horizon = merge(local.reverb_client_environment, {")
end = text.index("laravel-reverb = merge(", start)
block = text[start:end]
new = re.sub(r'^\s+FASTAPI_INTERNAL_URL = .*\n', "", block, count=1, flags=re.M)
if new == block:
    print("mutation matched nothing", file=sys.stderr); sys.exit(2)
open(path, "w", encoding="utf-8").write(text[:start] + new + text[end:])
PY
[ $? -eq 0 ] || exit 2
out="$(run_checker "$d")"; rc=$?
if [ "$rc" -eq 1 ] && grep -q "FASTAPI_INTERNAL_URL is set on" <<<"$out" && grep -q "NOT on laravel-horizon" <<<"$out" && ! grep -q "Helm chart" <<<"$out"; then
  ok "Horizon without FASTAPI_INTERNAL_URL in Terraform is rejected, without blaming the chart"
else
  bad "Terraform parity should still be enforced (rc=$rc)"; printf '%s\n' "$out" | sed 's/^/        /'
fi
rm -rf "$d"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
