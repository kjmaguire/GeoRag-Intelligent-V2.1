#!/usr/bin/env bash
# Discrimination tests for scripts/check-aws-power-flag.py.
#
# A guard that cannot fail is worse than no guard: it reports success over a
# deployment that bills around the clock while its owner believes it is off.
# Each case below is a mistake someone will plausibly make.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK="$ROOT/scripts/check-aws-power-flag.py"
PASS=0; FAIL=0
ok()   { printf 'ok   %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf 'FAIL %s\n' "$1"; FAIL=$((FAIL+1)); }

# run <expected-exit> <label> <<< heredoc of a .tf fixture
run() {
  local want="$1" label="$2" dir rc
  dir="$(mktemp -d)"; cat > "$dir/t.tf"
  python3 "$CHECK" "$dir" >/dev/null 2>&1; rc=$?
  rm -rf "$dir"
  [ "$rc" -eq "$want" ] && ok "$label" || bad "$label (exit $rc, want $want)"
}

run 0 "a gated load balancer passes" <<'TF'
resource "aws_lb" "this" {
  count = local.on
  name  = "x"
}
TF

run 1 "an UNGATED load balancer fails" <<'TF'
resource "aws_lb" "this" {
  name = "x"
}
TF

run 1 "a second NAT gateway added without the gate fails" <<'TF'
resource "aws_nat_gateway" "second_az" {
  allocation_id = aws_eip.nat[0].id
}
TF

run 0 "a gated for_each service passes" <<'TF'
resource "aws_ecs_service" "this" {
  for_each = local.on == 1 ? local.services : {}
  name     = each.key
}
TF

run 1 "an ungated for_each service fails" <<'TF'
resource "aws_ecs_service" "this" {
  for_each = local.services
  name     = each.key
}
TF

run 0 "an ungated S3 bucket passes -- state is kept" <<'TF'
resource "aws_s3_bucket" "this" {
  bucket = "x"
}
TF

run 1 "GATING the S3 bucket fails -- that destroys the corpus" <<'TF'
resource "aws_s3_bucket" "this" {
  count  = local.on
  bucket = "x"
}
TF

run 1 "gating the Secrets Manager secret fails -- 30-day recovery trap" <<'TF'
resource "aws_secretsmanager_secret" "app" {
  count = local.on
  name  = "x"
}
TF

run 1 "an unrecognised resource type must be classified" <<'TF'
resource "aws_elasticache_cluster" "surprise" {
  cluster_id = "x"
}
TF

run 0 "the committed tree passes" <<'TF'
TF
python3 "$CHECK" >/dev/null 2>&1 \
  && ok "the real terraform tree passes" \
  || bad "the real terraform tree passes"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
