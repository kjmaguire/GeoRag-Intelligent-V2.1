#!/usr/bin/env bash
# Discrimination tests for scripts/check_redis_manifests.py.
#
# A checker that prints OK against a tree someone has already fixed has
# demonstrated nothing. Each case below reproduces a shape that actually
# shipped, or that the rules exist to forbid, and asserts the checker
# rejects it -- and names which live defect it stands for.
#
# Run: bash scripts/tests/redis_manifests_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECKER="${REPO_ROOT}/scripts/check_redis_manifests.py"
PYTHON="${PYTHON:-python}"

# The production deployment moved from a Container Apps YAML to Terraform
# on 2026-09-08 (ADR-0022). The cases below moved with it: they are the
# same shapes, expressed in HCL, and they still name the live defect each
# one stands for.
TF="deploy/aws/terraform/services.tf"
TF_SIZING="deploy/aws/terraform/main.tf"
K8S="kubernetes/manifests/k3s.yaml"
HELM_TPL="charts/georag/templates/redis.yaml"
HELM_VALS="charts/georag/values.yaml"
FILES=("$TF" "$TF_SIZING" "$K8S" "kubernetes/manifests/vanilla.yaml"
       "kubernetes/manifests/airgap.yaml" "$HELM_TPL" "$HELM_VALS")

PASS=0
FAIL=0

# Build a scratch copy of just the files the checker reads.
fixture() {
  local dir
  dir="$(mktemp -d)"
  local f
  for f in "${FILES[@]}"; do
    mkdir -p "${dir}/$(dirname "$f")"
    cp "${REPO_ROOT}/${f}" "${dir}/${f}"
  done
  printf '%s' "$dir"
}

# mutate <dir> <file> <sed-expression>...
# Applies the edits and FAILS LOUDLY if the file is unchanged. A sed that
# silently matches nothing turns a test into a no-op -- and a no-op test
# whose expectation is "ok" passes for entirely the wrong reason. This
# suite had exactly that bug on first run.
mutate() {
  local dir="$1" file="$2"; shift 2
  local before after
  before="$(cksum < "${dir}/${file}")"
  local expr
  for expr in "$@"; do sed -i "$expr" "${dir}/${file}"; done
  after="$(cksum < "${dir}/${file}")"
  if [ "$before" = "$after" ]; then
    printf 'FAIL  fixture mutation matched nothing: %s  [%s]\n' "$file" "$*"
    FAIL=$((FAIL + 1))
    return 1
  fi
  return 0
}

# assert <name> <expect: ok|fail> <expected substring> <dir>
assert() {
  local name="$1" expect="$2" needle="$3" dir="$4"
  local out rc
  out="$(GEORAG_REPO_ROOT="$dir" "$PYTHON" "$CHECKER" 2>&1)"
  rc=$?
  rm -rf "$dir"

  local ok=1
  if [ "$expect" = "ok" ] && [ "$rc" -ne 0 ]; then ok=0; fi
  if [ "$expect" = "fail" ] && [ "$rc" -eq 0 ]; then ok=0; fi
  if [ -n "$needle" ] && ! printf '%s' "$out" | grep -qF "$needle"; then ok=0; fi

  if [ "$ok" -eq 1 ]; then
    PASS=$((PASS + 1))
    printf 'pass  %s\n' "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL  %s (rc=%s, wanted %s containing %q)\n' "$name" "$rc" "$expect" "$needle"
    printf '%s\n' "$out" | sed 's/^/        /'
  fi
}

# --- the tree as it stands -------------------------------------------
d="$(fixture)"
assert "unmodified tree passes" ok "All Redis manifests satisfy" "$d"

# --- rule 1, the defect that shipped to production --------------------
# The Azure container app carried --appendonly yes with no volume from the
# day of the lift until 2026-08-22 (deploy/azure/containerapps/redis.yaml,
# deleted 2026-09-08 — see git history), and the AWS deployment fixes it. Losing the mount must therefore break the check —
# otherwise nothing stands between the fix and a silent regression to it.
d="$(fixture)"
mutate "$d" "${TF}" 's|: "/data"|: "/var/lib/nothing"|'
assert "terraform: appendonly yes with no /data mount is rejected" fail \
  "cost without durability" "$d"

# The volume must be ATTACHED as well as mounted. Dropping redis from the
# dynamic block leaves a mountPoint pointing at nothing.
d="$(fixture)"
mutate "$d" "${TF}" 's/for_each = contains(\["qdrant", "redis"\], each.key)/for_each = contains(["qdrant"], each.key)/'
assert "terraform: a mount with no attached volume is rejected" fail \
  "cost without durability" "$d"

# The same rule has to catch RDB, not just AOF.
d="$(fixture)"
mutate "$d" "${TF}" 's|"--save", "",|"--save", "3600 1",|' 's|: "/data"|: "/var/lib/nothing"|'
assert "terraform: an active save policy with no volume is rejected" fail \
  "cost without durability" "$d"

# Dropping persistence entirely is not neutral, and it is exactly what
# Azure ran: AOF off and no volume, so every restart and every nightly
# scale-to-zero dropped all sessions and any queued Horizon job.
d="$(fixture)"
mutate "$d" "${TF}" 's/"--appendonly", "yes",/"--appendonly", "no",/' 's|"--save", "",|"--databases", "4",|'
assert "terraform: turning persistence back off is rejected" fail \
  "persistence is OFF" "$d"

# Rule 1 must also fire the other way round: persistence is legitimate
# where a volume exists, so removing the volume has to break it.
d="$(fixture)"
mutate "$d" "${K8S}" 's|mountPath: /data|mountPath: /var/lib/nothing|'
assert "k8s: appendonly yes after losing the volume is rejected" fail \
  "cost without durability" "$d"

# --- rule 2, both directions -----------------------------------------
# The live drift: maxmemory equal to the container limit. Redis's guard
# is unreachable because the platform kills the container first.
d="$(fixture)"
mutate "$d" "${TF}" 's/"--maxmemory", "384mb",/"--maxmemory", "1024mb",/'
assert "terraform: maxmemory == task memory is rejected" fail \
  "is unreachable" "$d"

# The case that must fail even though the two numbers LOOK different.
# Redis reads mb as binary, so 1024mb and a 1024 MiB task are the same
# 1073741824 bytes -- a decimal reading would let this pass.
d="$(fixture)"
mutate "$d" "${TF}" 's/"--maxmemory", "384mb",/"--maxmemory", "1024mb",/'
assert "terraform: redis 'mb' is parsed as binary, not decimal" fail \
  "at least 1280 MiB" "$d"

# The pre-2026-08-22 k8s state: a 2Gi limit and no cap at all.
d="$(fixture)"
mutate "$d" "${K8S}" '/--maxmemory 1536mb/d; s/--dir \/data \\/--dir \/data/'
mutate "$d" "${K8S}" '/--maxmemory-policy volatile-lru/d'
assert "k8s: no maxmemory under a memory limit is rejected" fail \
  "no --maxmemory is set" "$d"

# Just inside the boundary must pass, just outside must not: 409mb * 1.25
# = 511 MiB (fits 512), 410mb * 1.25 = 512.5 MiB (does not).
# Just inside the boundary must pass, just outside must not: 819mb * 1.25
# = 1023 MiB (fits 1024), 820mb * 1.25 = 1025 MiB (does not).
d="$(fixture)"
mutate "$d" "${TF}" 's/"--maxmemory", "384mb",/"--maxmemory", "819mb",/'
assert "terraform: 819mb is inside the headroom boundary" ok "" "$d"

d="$(fixture)"
mutate "$d" "${TF}" 's/"--maxmemory", "384mb",/"--maxmemory", "820mb",/'
assert "terraform: 820mb is outside the headroom boundary" fail \
  "is unreachable" "$d"

# --- the checker must not silently stop reading -----------------------
# If a manifest is restructured so a pattern no longer matches, that is a
# failure, not a pass. This is the failure mode the first draft of this
# checker actually had: it matched some other container's limits.
d="$(fixture)"
mutate "$d" "${TF_SIZING}" 's/redis          = { cpu = 512, memory = 1024/redis          = { cpu = 512, ram = 1024/'
assert "terraform: an unreadable task memory fails rather than passing" fail \
  "no longer reading it" "$d"

# And the same for the command itself: a restructured task definition must
# fail loudly rather than reporting OK against a list it never found.
d="$(fixture)"
mutate "$d" "${TF}" 's/^    redis = \[/    redis_server = [/'
assert "terraform: an unreadable command fails rather than passing" fail \
  "no longer reading it" "$d"

d="$(fixture)"
mutate "$d" "${HELM_VALS}" 's/^  maxmemory: "1536mb"/  maxmemoryCap: "1536mb"/'
assert "helm: a template with no matching values key is rejected" fail \
  "has no such key under 'redis:'" "$d"

# And it must resolve Helm through the redis section specifically -- an
# earlier draft read the first limits: block in values.yaml, which
# belongs to a different component entirely.
d="$(fixture)"
mutate "$d" "${HELM_VALS}" 's/^  maxmemory: "1536mb"/  maxmemory: "4096mb"/'
assert "helm: values are read from the redis section" fail \
  "is unreachable" "$d"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
