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

# ---------------------------------------------------------------------------
# Gating a resource the provider then refuses to delete. Both of these shipped
# in the original power flag and neither had ever run — there are no AWS
# credentials in CI, so `terraform plan` has never executed in either power
# state. A power-off would have failed AFTER destroying the ALB, the NAT
# gateway and every ECS service.
# ---------------------------------------------------------------------------

run 1 "a gated database with deletion_protection = true fails" <<'TF'
resource "aws_db_instance" "this" {
  count               = local.on
  deletion_protection = true
}
TF

run 0 "...but the same flag behind a variable passes" <<'TF'
resource "aws_db_instance" "this" {
  count               = local.on
  deletion_protection = var.db_deletion_protection
}
TF

run 0 "deletion_protection on an UNGATED resource is fine -- that is the seatbelt working" <<'TF'
resource "aws_ecs_cluster" "this" {
  deletion_protection = true
}
TF

run 1 "a constant final_snapshot_identifier fails -- the SECOND power-off collides" <<'TF'
locals {
  name = var.name_prefix
}

resource "aws_db_instance" "this" {
  count                     = local.on
  final_snapshot_identifier = "${local.name}-pg-final"
}
TF

run 0 "...a name carrying an operator-settable suffix passes" <<'TF'
locals {
  name = var.name_prefix
  snap = "${local.name}-pg-final-${var.db_final_snapshot_suffix}"
}

resource "aws_db_instance" "this" {
  count                     = local.on
  final_snapshot_identifier = local.snap
}
TF

run 1 "name_prefix alone does not count as varying -- it is deployment identity" <<'TF'
locals {
  name = "${var.name_prefix}-${var.region}"
  snap = "${local.name}-pg-final"
}

resource "aws_db_instance" "this" {
  count                     = local.on
  final_snapshot_identifier = local.snap
}
TF

run 1 "lifecycle.prevent_destroy on a gated resource fails -- the gate can never fire" <<'TF'
resource "aws_lb" "this" {
  count = local.on

  lifecycle {
    prevent_destroy = true
  }
}
TF

run 0 "the committed tree passes" <<'TF'
TF
python3 "$CHECK" >/dev/null 2>&1 \
  && ok "the real terraform tree passes" \
  || bad "the real terraform tree passes"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
