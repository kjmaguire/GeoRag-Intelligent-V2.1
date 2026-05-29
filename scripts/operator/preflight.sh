#!/usr/bin/env bash
# preflight.sh — verify operator setup is complete before first prod deploy.
#
# Read-only. Returns non-zero if any O-01..O-07 item fails.
# Pass --emit-cd-patch to print a unified diff that removes continue-on-error
# guards from cd.yml.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

EMIT_CD_PATCH=0
[[ "${1:-}" == "--emit-cd-patch" ]] && EMIT_CD_PATCH=1

c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn() { printf '\033[32m%s\033[0m\n' "$*"; }
c_yel() { printf '\033[33m%s\033[0m\n' "$*"; }
c_blu() { printf '\033[34m%s\033[0m\n' "$*"; }

PASS=0
FAIL=0

check() {
  local id="$1" desc="$2" status="$3" detail="${4:-}"
  if [ "$status" = "ok" ]; then
    c_grn "✓ ${id}  ${desc}"
    [ -n "$detail" ] && echo "       ${detail}"
    PASS=$((PASS+1))
  elif [ "$status" = "warn" ]; then
    c_yel "⚠ ${id}  ${desc}"
    [ -n "$detail" ] && echo "       ${detail}"
  else
    c_red "✗ ${id}  ${desc}"
    [ -n "$detail" ] && echo "       ${detail}"
    FAIL=$((FAIL+1))
  fi
}

emit_cd_patch() {
  if [ ! -f .github/workflows/cd.yml ]; then
    c_red "cd.yml missing; cannot emit patch"
    exit 1
  fi
  c_blu "# Patch removes 9 'continue-on-error: true' guards plus their TODO comments."
  c_blu "# Apply with: git apply <(bash scripts/operator/preflight.sh --emit-cd-patch)"
  c_blu ""
  # Use python for a robust line-level transform with diff output.
  python3 - <<'PY'
import difflib, pathlib, re
p = pathlib.Path('.github/workflows/cd.yml')
src = p.read_text().splitlines(keepends=True)
out = []
skip_next_blank = False
i = 0
while i < len(src):
    line = src[i]
    # Drop "continue-on-error: true" lines and the TODO comments immediately above them.
    if re.match(r'^\s*continue-on-error:\s*true\s*$', line):
        # Walk backwards in `out` and drop trailing TODO/continue-on-error comment block.
        while out and re.match(r'^\s*#\s*(TODO|continue-on-error).*$', out[-1]):
            out.pop()
        i += 1
        continue
    out.append(line)
    i += 1
diff = difflib.unified_diff(src, out, fromfile='a/.github/workflows/cd.yml',
                            tofile='b/.github/workflows/cd.yml', n=3)
import sys
sys.stdout.writelines(diff)
PY
}

if [ "$EMIT_CD_PATCH" = "1" ]; then
  emit_cd_patch
  exit 0
fi

c_blu "GeoRAG operator preflight"
c_blu "Repo: ${REPO_ROOT}"
echo

# ---------------------------------------------------------------------------
# O-01 — SOPS_AGE_PRIVATE_KEY in GitHub Secrets
# ---------------------------------------------------------------------------
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if gh secret list --json name -q '.[].name' 2>/dev/null | grep -qx 'SOPS_AGE_PRIVATE_KEY'; then
    check "O-01" "SOPS_AGE_PRIVATE_KEY GitHub Secret set" ok
  else
    check "O-01" "SOPS_AGE_PRIVATE_KEY GitHub Secret set" fail \
      "Run: bash scripts/operator/set-github-secrets.sh"
  fi
else
  check "O-01" "SOPS_AGE_PRIVATE_KEY GitHub Secret set" warn \
    "gh CLI not authenticated; cannot verify (run: gh auth login)"
fi

# .sops.yaml must exist with at least one age recipient
if [ -f .sops.yaml ] && grep -qE '^\s*age:' .sops.yaml; then
  rcpts=$(grep -oE 'age1[a-z0-9]{55,62}' .sops.yaml | wc -l)
  check "O-01b" ".sops.yaml present with ${rcpts} age recipient(s)" ok
else
  check "O-01b" ".sops.yaml present with age recipients" fail \
    "Run: bash scripts/operator/bootstrap-secrets.sh"
fi

# ---------------------------------------------------------------------------
# O-02 — SSH host secrets per environment
# ---------------------------------------------------------------------------
for ENV in dev staging production; do
  UPPER=$(echo "$ENV" | tr '[:lower:]' '[:upper:]')
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    set_count=0
    for KEY in "${UPPER}_SSH_HOST" "${UPPER}_SSH_USER" "${UPPER}_SSH_KEY"; do
      if gh secret list --env "$ENV" --json name -q '.[].name' 2>/dev/null | grep -qx "$KEY"; then
        set_count=$((set_count+1))
      fi
    done
    if [ "$set_count" = "3" ]; then
      check "O-02:${ENV}" "${ENV} SSH host trio set" ok
    elif [ "$set_count" = "0" ]; then
      check "O-02:${ENV}" "${ENV} SSH host trio set" fail "0/3 secrets set"
    else
      check "O-02:${ENV}" "${ENV} SSH host trio set" fail "${set_count}/3 secrets set"
    fi
  else
    check "O-02:${ENV}" "${ENV} SSH host trio set" warn "gh CLI unavailable"
  fi
done

# ---------------------------------------------------------------------------
# O-03 — STAGING_URL
# ---------------------------------------------------------------------------
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if gh secret list --json name -q '.[].name' 2>/dev/null | grep -qx 'STAGING_URL'; then
    check "O-03" "STAGING_URL GitHub Secret set" ok
  else
    check "O-03" "STAGING_URL GitHub Secret set" fail \
      "Required by perf-baseline.yml + release-rehearsal.yml"
  fi
else
  check "O-03" "STAGING_URL GitHub Secret set" warn "gh CLI unavailable"
fi

# ---------------------------------------------------------------------------
# O-04 — Encrypted .env.production.enc committed
# ---------------------------------------------------------------------------
if [ -f .env.production.enc ]; then
  if head -20 .env.production.enc | grep -q '"sops"'; then
    check "O-04" ".env.production.enc present and SOPS-encrypted" ok
  else
    check "O-04" ".env.production.enc present and SOPS-encrypted" fail \
      "File present but does not look like SOPS output"
  fi
else
  check "O-04" ".env.production.enc present and SOPS-encrypted" fail \
    "Run: bash scripts/operator/bootstrap-secrets.sh"
fi

# Plaintext .env.production must NOT exist
if [ -f .env.production ]; then
  check "O-04b" "no plaintext .env.production in repo" fail \
    "Plaintext file exists — encrypt it with sops and delete the plaintext"
else
  check "O-04b" "no plaintext .env.production in repo" ok
fi

# ---------------------------------------------------------------------------
# O-05 — Cold-start runbook present (operator must execute on host)
# ---------------------------------------------------------------------------
if [ -f ops/runbooks/cold-start.md ]; then
  check "O-05" "cold-start.md runbook available" ok \
    "Operator must execute on the prod host; this script cannot verify remote state."
else
  check "O-05" "cold-start.md runbook available" fail "missing"
fi

# ---------------------------------------------------------------------------
# O-06 — Perf-baseline doc has values (not PENDING)
# ---------------------------------------------------------------------------
if [ -f ops/baselines/2026-04-22-api-latency.md ]; then
  if grep -qiE '\bPENDING\b' ops/baselines/2026-04-22-api-latency.md; then
    check "O-06" "first perf-baseline captured" warn \
      "baseline file still contains PENDING — fires after first nightly run"
  else
    check "O-06" "first perf-baseline captured" ok
  fi
else
  check "O-06" "first perf-baseline captured" fail "baseline doc missing"
fi

# ---------------------------------------------------------------------------
# O-07 — Alertmanager prod template + actual prod config presence
# ---------------------------------------------------------------------------
if [ -f docker/alertmanager/alertmanager.production.yml.example ]; then
  check "O-07" "alertmanager.production.yml.example template present" ok \
    "Operator copies + substitutes on the prod host (not committed)"
else
  check "O-07" "alertmanager.production.yml.example template present" fail "missing"
fi

# ---------------------------------------------------------------------------
# Bonus: cd.yml continue-on-error guards still present?
# ---------------------------------------------------------------------------
if [ -f .github/workflows/cd.yml ]; then
  guards=$(grep -c '^\s*continue-on-error:\s*true' .github/workflows/cd.yml || true)
  if [ "$guards" = "0" ]; then
    check "CI" "cd.yml continue-on-error guards removed" ok
  else
    check "CI" "cd.yml continue-on-error guards removed" warn \
      "${guards} guard(s) still present — once O-01..O-03 pass, run: bash scripts/operator/preflight.sh --emit-cd-patch | git apply"
  fi
fi

# ---------------------------------------------------------------------------
# Bonus: APP_DEBUG sanity in .env.production.example
# ---------------------------------------------------------------------------
if grep -qE '^APP_DEBUG=false' .env.production.example; then
  check "ENV" ".env.production.example mandates APP_DEBUG=false" ok
else
  check "ENV" ".env.production.example mandates APP_DEBUG=false" fail \
    "Template should hard-code APP_DEBUG=false"
fi

echo
if [ "$FAIL" = "0" ]; then
  c_grn "═══════════════════════════════════════════════════════════════════"
  c_grn "  ALL CHECKS PASSED — V1 ship-readiness gates green."
  c_grn "═══════════════════════════════════════════════════════════════════"
  exit 0
else
  c_red "═══════════════════════════════════════════════════════════════════"
  c_red "  ${PASS} passed, ${FAIL} failed — see docs/OPERATOR-AFTERNOON.md"
  c_red "═══════════════════════════════════════════════════════════════════"
  exit 1
fi
