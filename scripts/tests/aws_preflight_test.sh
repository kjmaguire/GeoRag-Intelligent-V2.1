#!/usr/bin/env bash
# Discrimination tests for scripts/operator/aws-preflight.sh.
#
# A gate that passes against the tree it was written on has demonstrated
# nothing. Each case below builds a fixture repository in the shape the check
# exists to reject, and asserts the gate rejects it -- and names the live
# failure that shape produces.
#
# The AWS-side checks (A-06..A-10, A-12) are not exercised: they need a real
# account. What IS asserted about them is the property that matters when no
# account is reachable -- that they report unverified rather than passing.
#
# Run: bash scripts/tests/aws_preflight_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATE="${REPO_ROOT}/scripts/operator/aws-preflight.sh"

PASS=0
FAIL=0

ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '\033[31m  ✗ %s\033[0m\n' "$*"; FAIL=$((FAIL+1)); }
case_() { printf '\033[34m%s\033[0m\n' "$*"; }

# Build a minimal fixture repo that the gate passes cleanly, so each test can
# introduce exactly one defect and attribute the failure to it.
make_fixture() {
  local d
  d="$(mktemp -d)"
  mkdir -p "$d/scripts/operator" "$d/scripts" "$d/deploy/aws/terraform" \
           "$d/ops/validation/reports"
  cp "$GATE" "$d/scripts/operator/aws-preflight.sh"

  cat >"$d/deploy/aws/terraform/config.tf" <<'TF'
variable "acm_certificate_arn" {
  type = string
}

variable "with_default" {
  type    = string
  default = "fine"
}

locals {
  env = {
    GEORAG_ENV = "production"
  }
}
TF

  cat >"$d/deploy/aws/README.md" <<'MD'
| Key | Read by | What it is |
| --- | --- | --- |
| `APP_KEY` | laravel | key |
| `REDIS_PASSWORD` | all | pw |
| `APP_KEY_NEXT` | rotation only | **Not a go-live key.** |
MD

  # Stand-in for the real key checker; A-02 only cares about its exit status.
  cat >"$d/scripts/check-ecs-secret-keys.py" <<'PY'
import sys
sys.exit(0)
PY

  echo '{}' >"$d/ops/validation/reports/bedrock_probe_20260915T000000Z.json"

  git -C "$d" init -q
  git -C "$d" config user.email t@t.t
  git -C "$d" config user.name t
  git -C "$d" add -A >/dev/null 2>&1
  git -C "$d" commit -qm fixture >/dev/null 2>&1
  echo "$d"
}

# Run the gate in a fixture with all no-default vars supplied.
run_gate() {
  local d="$1"; shift
  ( cd "$d" && TF_VAR_acm_certificate_arn=arn:aws:acm:x \
      bash scripts/operator/aws-preflight.sh 2>&1 )
}

strip_ansi() { sed -E 's/\x1b\[[0-9;]*m//g'; }

# ---------------------------------------------------------------------------
case_ "baseline — a fixture with every repo-side precondition met"
D=$(make_fixture)
OUT=$(run_gate "$D" | strip_ansi)
if grep -qE '^✓ A-01' <<<"$OUT"; then ok "A-01 passes when TF_VAR_ supplies the value"; else bad "A-01 should pass; got: $(grep A-01 <<<"$OUT" | head -1)"; fi
if grep -qE '^✓ A-03' <<<"$OUT"; then ok "A-03 passes with GEORAG_ENV=production"; else bad "A-03 should pass"; fi
if grep -qE '^✓ A-05' <<<"$OUT"; then ok "A-05 passes with nothing sensitive tracked"; else bad "A-05 should pass"; fi
if grep -qE '^✓ A-11' <<<"$OUT"; then ok "A-11 passes with a probe report present"; else bad "A-11 should pass"; fi
if ! grep -qE '^✗' <<<"$OUT"; then ok "no failures on a clean fixture"; else bad "clean fixture produced: $(grep '^✗' <<<"$OUT" | tr '\n' ' ')"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "A-01 — a variable with no default and no value"
# Live failure: terraform apply blocks on an interactive prompt, or in CI fails
# outright. Six such variables shipped in this repository.
D=$(make_fixture)
OUT=$( cd "$D" && bash scripts/operator/aws-preflight.sh 2>&1 | strip_ansi )   # no TF_VAR_
if grep -qE '^✗ A-01' <<<"$OUT" && grep -q 'acm_certificate_arn' <<<"$OUT"; then
  ok "rejects an unsupplied no-default variable, and names it"
else
  bad "should have failed A-01 naming acm_certificate_arn"
fi
if ! grep -q 'with_default' <<<"$OUT"; then
  ok "does not flag a variable that has a default"
else
  bad "flagged with_default, which has a default"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "A-03 — GEORAG_ENV not pinned to production"
# Live failure: main.py::_assert_production_posture is the only thing that
# reports a security control being off, and it never runs.
D=$(make_fixture)
sed -i 's/GEORAG_ENV = "production"/GEORAG_ENV = "staging"/' "$D/deploy/aws/terraform/config.tf"
OUT=$(run_gate "$D" | strip_ansi)
if grep -qE '^✗ A-03' <<<"$OUT"; then ok "rejects GEORAG_ENV != production"; else bad "should have failed A-03"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "A-05 — Terraform state committed to git"
# Live failure: state holds every value it touches in PLAINTEXT, including the
# RDS master password. Permanent once pushed. This repository has already
# committed one live credential, so the shape is demonstrated, not theoretical.
D=$(make_fixture)
echo '{"outputs":{"db_password":{"value":"hunter2"}}}' >"$D/deploy/aws/terraform/terraform.tfstate"
git -C "$D" add -f deploy/aws/terraform/terraform.tfstate >/dev/null 2>&1
git -C "$D" commit -qm leak >/dev/null 2>&1
OUT=$(run_gate "$D" | strip_ansi)
if grep -qE '^✗ A-05' <<<"$OUT" && grep -q 'terraform.tfstate' <<<"$OUT"; then
  ok "rejects committed state, and names the file"
else
  bad "should have failed A-05 naming terraform.tfstate"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "A-05 — a tfvars file committed"
# Live failure: the six no-default values are supplied here, and a file filled
# in under time pressure is where a password gets pasted by mistake.
D=$(make_fixture)
echo 'acm_certificate_arn = "arn:aws:acm:secret"' >"$D/deploy/aws/terraform/prod.tfvars"
git -C "$D" add -f deploy/aws/terraform/prod.tfvars >/dev/null 2>&1
git -C "$D" commit -qm leak >/dev/null 2>&1
OUT=$(run_gate "$D" | strip_ansi)
if grep -qE '^✗ A-05' <<<"$OUT"; then ok "rejects a committed .tfvars"; else bad "should have failed A-05"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "A-05 — .tfvars.example stays allowed"
# A template carries key names and no values; forbidding it would push
# operators toward undocumented ad-hoc files.
D=$(make_fixture)
echo 'acm_certificate_arn = ""' >"$D/deploy/aws/terraform/prod.tfvars.example"
git -C "$D" add -f deploy/aws/terraform/prod.tfvars.example >/dev/null 2>&1
git -C "$D" commit -qm tmpl >/dev/null 2>&1
OUT=$(run_gate "$D" | strip_ansi)
if grep -qE '^✓ A-05' <<<"$OUT"; then ok "allows a committed .tfvars.example"; else bad "should not flag .tfvars.example"; fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "A-11 — no Bedrock probe report"
# Live failure: every Bedrock adapter was written to documentation and never
# verified. The path this replaced had three behaviours documentation got
# WRONG, settled only by a live call.
D=$(make_fixture)
rm -f "$D"/ops/validation/reports/bedrock_probe_*.json
OUT=$(run_gate "$D" | strip_ansi)
if grep -qE '^✗ A-11' <<<"$OUT" && grep -q 'ASSUMED' <<<"$OUT"; then
  ok "rejects a tree with no probe report, and says the shapes are assumed"
else
  bad "should have failed A-11"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "A-10 — go-live key derivation excludes APP_KEY_NEXT"
# Live failure in the other direction: rotation.tf relies on APP_KEY_NEXT being
# ABSENT to make the rotation task unrunnable outside a rotation. A gate that
# demanded it would have the operator create it and silently arm that task.
D=$(make_fixture)
OUT=$(run_gate "$D" | strip_ansi)
if grep -qE '^⚠ A-10' <<<"$OUT" && grep -q '2 keys expected' <<<"$OUT"; then
  ok "counts 2 go-live keys from a 3-row table, excluding APP_KEY_NEXT"
else
  bad "expected A-10 unverified with 2 keys; got: $(grep -A1 'A-10' <<<"$OUT" | tr '\n' ' ')"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
case_ "unanswerable AWS checks report unverified, never pass"
# The property that matters off-account: silence is not assurance.
D=$(make_fixture)
OUT=$(run_gate "$D" | strip_ansi)
for id in A-06 A-07 A-08 A-09 A-10; do
  if grep -qE "^⚠ ${id}" <<<"$OUT"; then
    ok "${id} reports unverified without AWS"
  elif grep -qE "^✓ ${id}" <<<"$OUT"; then
    bad "${id} PASSED without an account — that is a false green"
  else
    bad "${id} missing from output"
  fi
done
if grep -q 'UNVERIFIED — an unanswered check is not a pass' <<<"$OUT"; then
  ok "summary distinguishes unverified from passed"
else
  bad "summary should flag unverified checks"
fi
rm -rf "$D"

# ---------------------------------------------------------------------------
echo
if [ "$FAIL" = "0" ]; then
  printf '\033[32m%s passed, 0 failed\033[0m\n' "$PASS"
  exit 0
else
  printf '\033[31m%s passed, %s FAILED\033[0m\n' "$PASS" "$FAIL"
  exit 1
fi
