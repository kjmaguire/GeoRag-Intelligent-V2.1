#!/usr/bin/env bash
# Dry rehearsal of the two image-upgrade scripts against a fake `aws`.
#
#   image_*    upgrade-service-image.sh: a dry run changes nothing, --apply
#              changes ONLY the image (secrets, env, volumes carried over
#              from the service's own revision), qdrant is refused, the pin
#              is read from services.tf.
#   qdrant_*   upgrade-qdrant.sh: a hop that skips a minor version is
#              refused before anything happens; nothing moves before the
#              snapshot succeeds; a changed point count stops the run.
#
# Usage: bash deploy/aws/upgrade/tests/run.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UP="$(dirname "$HERE")"
IMAGE_SH="$UP/upgrade-service-image.sh"
QDRANT_SH="$UP/upgrade-qdrant.sh"
SERVICES_TF="$(cd "$UP/../terraform" && pwd)/services.tf"

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq is not installed" >&2; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
cp "$HERE/fake-aws" "$WORK/bin/aws"
chmod +x "$WORK/bin/aws"

PASS=0
FAIL=0
CURRENT=""
OUT=""
RC=0
ST=""

ok()  { PASS=$((PASS + 1)); }
bad() { printf '  FAIL  %s: %s\n' "$CURRENT" "$1"; FAIL=$((FAIL + 1)); }
check() { if eval "$2"; then ok; else bad "$1"; printf '        --- output ---\n'; sed 's/^/        /' <<<"$OUT" | tail -20; fi; }

Q17="qdrant/qdrant:v1.17.1@sha256:1111111111111111111111111111111111111111111111111111111111111111"
Q18="qdrant/qdrant:v1.18.3@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286"
Q19="qdrant/qdrant:v1.19.1@sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10"
Q192="qdrant/qdrant:v1.19.2@sha256:2222222222222222222222222222222222222222222222222222222222222222"
H_OLD="ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.86.12@sha256:aaaa"
H_NEW="ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.91.2@sha256:bbbb"

new_case() {
  CURRENT="$1"
  ST="$WORK/$(printf '%s' "$1" | tr -c 'A-Za-z0-9' '_')"
  mkdir -p "$ST"
  : > "$ST/calls"
  # Service revisions deliberately carry secrets, env and a volume: the
  # property under test is that ONLY the image changes.
  jq -n --arg img "$H_OLD" '{family: "georag-hatchet", taskDefinitionArn: "georag-hatchet:7", revision: 7,
      status: "ACTIVE", registeredAt: "x", requiresAttributes: [], compatibilities: ["FARGATE"],
      containerDefinitions: [{name: "hatchet", image: $img,
        secrets: [{name: "DATABASE_URL", valueFrom: "arn:secret:HATCHET_DATABASE_URL::"}],
        environment: [{name: "SERVER_GRPC_PORT", value: "7077"}],
        mountPoints: [{sourceVolume: "config", containerPath: "/config"}]}],
      volumes: [{name: "config"}]}' > "$ST/td.georag-hatchet:7.json"
  echo "georag-hatchet:7" > "$ST/svc.hatchet"
  jq -n --arg img "${2:-$Q17}" '{family: "georag-qdrant", taskDefinitionArn: "georag-qdrant:3", revision: 3,
      containerDefinitions: [{name: "qdrant", image: $img,
        secrets: [{name: "QDRANT__SERVICE__API_KEY", valueFrom: "arn:secret:QDRANT_API_KEY::"}]}]}' \
    > "$ST/td.georag-qdrant:3.json"
  echo "georag-qdrant:3" > "$ST/svc.qdrant"
  v="${2:-$Q17}"; v="${v##*:v}"; echo "${v%%@*}" > "$ST/qdrant.version"
  echo '{"georag_chunks": 18, "other": 5}' > "$ST/counts.json"
}

run() {
  OUT=$(cd "$ST" && env -i PATH="$WORK/bin:$PATH" FAKE_STATE="$ST" \
          FAKE_DESIRED="${FAKE_DESIRED:-1}" FAKE_UNSTABLE="${FAKE_UNSTABLE:-0}" \
          FAKE_SNAPSHOT_FAIL="${FAKE_SNAPSHOT_FAIL:-0}" FAKE_LOSE_POINTS="${FAKE_LOSE_POINTS:-}" \
          bash "$@" 2>&1)
  RC=$?
}
moved() { grep -q "^ecs update-service" "$ST/calls"; }
running_image() { jq -r --arg c "$2" '.containerDefinitions[] | select(.name == $c) | .image' "$ST/td.$(cat "$ST/svc.$1").json"; }

echo "upgrade-service-image.sh"
# ─────────────────────────────────────────────────────────────────────────────
new_case "image_dry_run_changes_nothing"
run "$IMAGE_SH" hatchet "$H_NEW"
check "exit 0" '[ "$RC" -eq 0 ]'
check "shows before and after" 'grep -q "image now:      $H_OLD" <<<"$OUT" && grep -q "image after:    $H_NEW" <<<"$OUT"'
check "nothing registered or moved" '! grep -qE "register-task-definition|update-service" "$ST/calls"'

new_case "image_apply_changes_only_the_image"
run "$IMAGE_SH" hatchet "$H_NEW" --apply
check "exit 0" '[ "$RC" -eq 0 ]'
check "service runs the new image" '[ "$(running_image hatchet hatchet)" = "$H_NEW" ]'
check "and nothing else changed" \
  '[ "$(jq -S "del(.taskDefinitionArn, .revision, .status, .registeredAt, .requiresAttributes, .compatibilities) | .containerDefinitions[0].image = null" "$ST/td.$(cat "$ST/svc.hatchet").json")" = "$(jq -S "del(.taskDefinitionArn, .revision, .status, .registeredAt, .requiresAttributes, .compatibilities) | .containerDefinitions[0].image = null" "$ST/td.georag-hatchet:7.json")" ]'
check "prints the rollback" 'grep -q "task-definition georag-hatchet:7" <<<"$OUT"'
check "reports OK" 'grep -q "UPGRADE OK" <<<"$OUT"'

new_case "image_already_there_is_a_no_op"
run "$IMAGE_SH" hatchet "$H_OLD" --apply
check "exit 0, nothing moved" '[ "$RC" -eq 0 ] && ! moved'

new_case "image_scaled_to_zero_does_not_wait"
FAKE_DESIRED=0 run "$IMAGE_SH" hatchet "$H_NEW" --apply
check "exit 0" '[ "$RC" -eq 0 ]'
check "does not wait for a task that will not start" '! grep -q "wait services-stable" "$ST/calls"'

new_case "image_unstable_is_not_ok"
FAKE_UNSTABLE=1 run "$IMAGE_SH" hatchet "$H_NEW" --apply
check "exit 1" '[ "$RC" -eq 1 ] && ! grep -q "UPGRADE OK" <<<"$OUT"'

new_case "image_refuses_qdrant"
run "$IMAGE_SH" qdrant "$Q19" --apply
check "exit 2, nothing touched" '[ "$RC" -eq 2 ] && [ ! -s "$ST/calls" ] && grep -q upgrade-qdrant.sh <<<"$OUT"'

new_case "image_defaults_to_the_services_tf_pin"
PIN=$(awk '/external_image = \{/{f=1;next} f&&/^  \}/{exit} f&&$1=="hatchet"&&$2=="="{gsub(/"/,"",$3);print $3;exit}' "$SERVICES_TF")
run "$IMAGE_SH" hatchet
check "found a pin in services.tf" '[ -n "$PIN" ]'
check "uses it" 'grep -q "image after:    $PIN" <<<"$OUT"'

echo "upgrade-qdrant.sh"
# ─────────────────────────────────────────────────────────────────────────────
new_case "qdrant_dry_run_changes_nothing"
run "$QDRANT_SH" "$Q18" "$Q19"
check "exit 0" '[ "$RC" -eq 0 ]'
check "no task, no move" '! grep -qE "run-task|update-service|register" "$ST/calls"'

new_case "qdrant_refuses_a_skipped_minor_version"
run "$QDRANT_SH" "$Q19" --apply
check "exit 1" '[ "$RC" -eq 1 ] && grep -q "skips a minor version" <<<"$OUT"'
check "before any snapshot or move" '! grep -qE "run-task|update-service" "$ST/calls"'

new_case "qdrant_refuses_an_unpinned_hop"
run "$QDRANT_SH" "qdrant/qdrant:v1.18.3" --apply
check "exit 2" '[ "$RC" -eq 2 ] && grep -q "not digest-pinned" <<<"$OUT"'

new_case "qdrant_refuses_out_of_order_hops"
run "$QDRANT_SH" "$Q19" "$Q18" --apply
check "exit 1" '[ "$RC" -eq 1 ] && ! moved'

new_case "qdrant_two_hops_happy_path"
run "$QDRANT_SH" "$Q18" "$Q19" --apply
check "exit 0" '[ "$RC" -eq 0 ]'
check "ends on 1.19.1" '[ "$(running_image qdrant qdrant)" = "$Q19" ]'
check "went through 1.18.3" 'grep -q "move to v1.18.3" <<<"$OUT"'
first_task=$(grep -n "^ecs run-task" "$ST/calls" | head -1 | cut -d: -f1)
first_move=$(grep -n "^ecs update-service" "$ST/calls" | head -1 | cut -d: -f1)
check "snapshot BEFORE the first move" '[ "$first_task" -lt "$first_move" ]'
check "secrets carried over" '[ "$(jq -r ".containerDefinitions[0].secrets[0].name" "$ST/td.$(cat "$ST/svc.qdrant").json")" = "QDRANT__SERVICE__API_KEY" ]'
check "reports OK" 'grep -q "QDRANT UPGRADE OK" <<<"$OUT"'

new_case "qdrant_skips_hops_already_behind"
new_case "qdrant_skips_hops_already_behind" "$Q18"
run "$QDRANT_SH" "$Q18" "$Q19" --apply
check "exit 0" '[ "$RC" -eq 0 ]'
check "did not move to 1.18 again" '! grep -q "move to v1.18.3" <<<"$OUT"'
check "ends on 1.19.1" '[ "$(running_image qdrant qdrant)" = "$Q19" ]'

new_case "qdrant_patch_hop_is_allowed" "$Q19"
run "$QDRANT_SH" "$Q192" --apply
check "exit 0" '[ "$RC" -eq 0 ] && [ "$(running_image qdrant qdrant)" = "$Q192" ]'

new_case "qdrant_snapshot_failure_moves_nothing"
FAKE_SNAPSHOT_FAIL=1 run "$QDRANT_SH" "$Q18" --apply
check "exit 1" '[ "$RC" -eq 1 ] && grep -q "SNAPSHOT FAILED" <<<"$OUT"'
check "nothing moved" '! moved'

new_case "qdrant_lost_points_stop_the_run"
FAKE_LOSE_POINTS=1.18.3 run "$QDRANT_SH" "$Q18" "$Q19" --apply
check "exit 1" '[ "$RC" -eq 1 ] && grep -q "POINT COUNTS CHANGED" <<<"$OUT"'
check "did not go on to 1.19" '[ "$(running_image qdrant qdrant)" = "$Q18" ]'
check "prints how to go back" 'grep -q "task-definition georag-qdrant:3" <<<"$OUT"'

new_case "qdrant_scaled_to_zero_is_refused"
FAKE_DESIRED=0 run "$QDRANT_SH" "$Q18" --apply
check "exit 1, nothing moved" '[ "$RC" -eq 1 ] && ! moved'

echo
if [ "$FAIL" -eq 0 ]; then
  echo "$PASS checks passed"
else
  echo "$FAIL FAILED, $PASS passed"
  exit 1
fi
