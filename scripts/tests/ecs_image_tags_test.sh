#!/usr/bin/env bash
# Discrimination tests for scripts/check-ecs-image-tags.py.
#
# The rule is narrow on purpose: a tag pulled from THIS deployment's ECR must
# come from a variable, because those repositories are IMMUTABLE. A pinned tag
# on a third-party registry is the opposite — correct, and must not fire.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK="$ROOT/scripts/check-ecs-image-tags.py"
PASS=0; FAIL=0
ok()  { printf 'ok   %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf 'FAIL %s\n' "$1"; FAIL=$((FAIL+1)); }

run() {
  local want="$1" label="$2" dir rc
  dir="$(mktemp -d)"; cat > "$dir/t.tf"
  python3 "$CHECK" "$dir" >/dev/null 2>&1; rc=$?
  rm -rf "$dir"
  [ "$rc" -eq "$want" ] && ok "$label" || bad "$label (exit $rc, want $want)"
}

run 1 "the :latest that shipped is rejected" <<'TF'
resource "aws_ecs_task_definition" "x" {
  container_definitions = jsonencode([{
    image = "${aws_ecr_repository.this["laravel"].repository_url}:latest"
  }])
}
TF

run 0 "...the same reference through var.image_tag passes" <<'TF'
resource "aws_ecs_task_definition" "x" {
  container_definitions = jsonencode([{
    image = "${aws_ecr_repository.this["laravel"].repository_url}:${var.image_tag}"
  }])
}
TF

run 1 "a pinned SHA hardcoded in the tree is rejected too -- it goes stale silently" <<'TF'
resource "aws_ecs_task_definition" "x" {
  container_definitions = jsonencode([{
    image = "${aws_ecr_repository.this["laravel"].repository_url}:a4ed53a"
  }])
}
TF

run 0 "a pinned THIRD-PARTY image passes -- that registry does move its tags" <<'TF'
locals {
  external_image = {
    qdrant = "qdrant/qdrant:v1.17.1"
    redis  = "redis:8.10.0-alpine"
  }
}
TF

run 0 "the aws-cli image the sweeps run passes -- not our ECR" <<'TF'
resource "aws_ecs_task_definition" "sweep" {
  container_definitions = jsonencode([{
    image = "public.ecr.aws/aws-cli/aws-cli:latest"
  }])
}
TF

run 0 "the lookup() form the ten services share passes" <<'TF'
resource "aws_ecs_task_definition" "x" {
  container_definitions = jsonencode([{
    image = lookup(
      local.external_image,
      each.key,
      "${aws_ecr_repository.this[lookup(local.service_image, each.key, "fastapi")].repository_url}:${var.image_tag}",
    )
  }])
}
TF

python3 "$CHECK" >/dev/null 2>&1 \
  && ok "the real terraform tree passes" \
  || bad "the real terraform tree passes"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
